"""Search orchestration with persistent quotas and bounded external requests."""

import os
import re
import time
from dataclasses import dataclass

import requests

from livedigit.discovery import (
    CATEGORIES, LOCATIONS, SOCIAL, STATE_TELUGU, Contacts, WebsiteReader,
    business_result, result_text, website_domain,
)
from livedigit.store import contact_fields, now

SERPER_URL = "https://google.serper.dev/search"


class SearchError(Exception):
    pass


class LimitReached(SearchError):
    pass


@dataclass(frozen=True)
class Settings:
    key: str
    db_path: str = "data/leads.sqlite3"
    credit_limit: int = 50
    daily_target: int = 1000
    interval: float = 1.0
    domains: frozenset[str] = frozenset()

    @classmethod
    def load(cls):
        limit = int(os.getenv("DAILY_API_CREDIT_LIMIT", "50"))
        target = int(os.getenv("DAILY_PROFILE_TARGET", "1000"))
        interval = float(os.getenv("SERPER_MIN_INTERVAL", "1"))
        if limit < 1 or target < 1 or not 0.25 <= interval <= 60:
            raise ValueError("Daily limits must be positive and SERPER_MIN_INTERVAL must be between 0.25 and 60.")
        return cls(
            os.getenv("SERPER_API_KEY", "").strip(), os.getenv("DB_PATH", "data/leads.sqlite3"),
            limit, target, interval,
            frozenset(host.strip().lower() for host in os.getenv("AUTHORIZED_WEBSITE_DOMAINS", "").split(",") if host.strip()),
        )


@dataclass(frozen=True)
class Search:
    state: str
    city: str
    category: str
    platforms: tuple[str, ...]
    languages: tuple[str, ...]
    keyword: str = ""
    pages: int = 1
    discover_websites: bool = False

    def queries(self):
        local_city = LOCATIONS[self.state].get(self.city, self.city)
        term = CATEGORIES[self.category]
        for platform in self.platforms:
            for language in self.languages:
                if language == "Telugu":
                    query = f"{term[1]} {local_city} {STATE_TELUGU[self.state]}"
                else:
                    query = f"{term[0]} {self.city} {self.state}"
                yield platform, language, f"site:{SOCIAL[platform]} {query} {self.keyword}".strip()


class Serper:
    def __init__(self, settings, store, session=None):
        self.settings = settings
        self.store = store
        self.session = session or requests.Session()

    def search(self, search_id, query, language, page):
        delay = self.store.reserve_request(
            search_id, self.settings.credit_limit, self.settings.daily_target, self.settings.interval,
        )
        if delay is None:
            raise LimitReached("Daily profile target or API request limit reached (India time).")
        if delay:
            time.sleep(delay)
        try:
            response = self.session.post(
                SERPER_URL, json={"q": query, "gl": "in", "hl": "te" if language == "Telugu" else "en", "page": page},
                headers={"X-API-KEY": self.settings.key}, timeout=(5, 15), allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise SearchError("Serper could not be reached. Check network access and try again later.") from exc
        if response.status_code in {401, 403}:
            raise SearchError("Serper refused authentication. Check SERPER_API_KEY and network permissions.")
        if response.status_code in {402, 429}:
            raise SearchError("Serper credit or rate limit reached. Check your account before trying again.")
        if response.status_code != 200:
            raise SearchError(f"Serper returned HTTP {response.status_code}.")
        try:
            data = response.json()
            results = data.get("organic", []) if isinstance(data, dict) else None
            if not isinstance(results, list) or (isinstance(data, dict) and data.get("message") and "organic" not in data):
                raise ValueError
        except ValueError as exc:
            raise SearchError("Serper returned an unexpected response.") from exc
        return [item for item in results if isinstance(item, dict)]


def discover_website(serper, search_id, name, city):
    """Use a website URL only when search evidence ties the domain to the business and city."""
    # Remove search operators from provider-supplied names before quoting.
    clean_name = re.sub(r'[^\w\s&.-]', " ", name).strip()[:100]
    if len(clean_name) < 3:
        return ""
    results = serper.search(search_id, f'"{clean_name}" {city} official website', "English", 1)
    excluded = {"justdial.com", "indiamart.com", "sulekha.com", "zomato.com", "swiggy.com",
                "tripadvisor.in", "tripadvisor.com", "magicpin.in", "practo.com"}
    for item in results:
        url = result_text(item, "link")
        host = website_domain(url)
        if not host or any(host == domain or host.endswith("." + domain) for domain in excluded):
            continue
        title = result_text(item, "title").casefold()
        snippet = result_text(item, "snippet").casefold()
        compact_name = re.sub(r"[^\w]", "", clean_name.casefold())
        compact_host = re.sub(r"[^\w]", "", host.removeprefix("www.").rsplit(".", 1)[0])
        if (compact_name and compact_name in compact_host and clean_name.casefold() in title
                and city.casefold() in f"{title} {snippet}"):
            return url
    return ""


def run_search(spec, settings, store, progress=lambda message, done, total: None, serper=None, reader=None):
    if not settings.key:
        raise SearchError("Set SERPER_API_KEY before running a search.")
    if not spec.platforms or not spec.languages or not spec.city.strip() or not 1 <= spec.pages <= 5:
        raise SearchError("Choose at least one platform and language, a city, and 1–5 pages.")
    serper = serper or Serper(settings, store)
    reader = reader or WebsiteReader(settings.domains)
    queries = list(spec.queries())
    local_city = LOCATIONS[spec.state].get(spec.city, spec.city)
    total = len(queries) * spec.pages
    description = " · ".join((spec.city, spec.state, spec.category, ", ".join(spec.platforms),
                              ", ".join(spec.languages), f"{spec.pages} page(s)", spec.keyword or "Default keywords"))
    search_id = store.start(description)
    status = "Completed"
    matches = completed = 0
    seen = {}
    websites = {}
    deferred_error = None
    try:
        for platform, language, query in queries:
            for page in range(1, spec.pages + 1):
                progress(f"Searching {platform} in {language}, page {page}", completed, total)
                results = serper.search(search_id, query, language, page)
                for item in results:
                    parsed = business_result(item, platform, spec.category, spec.city, local_city, spec.keyword)
                    if not parsed:
                        continue
                    name, url, website, contact = parsed
                    previous = seen.get(url)
                    # Repeat snippets can contain contact details absent from the first result.
                    if previous is not None and all(
                        not getattr(contact, field) or getattr(previous, field)
                        for field in ("mobile", "whatsapp", "email")
                    ):
                        store.count_duplicate(search_id)
                        continue
                    if spec.discover_websites and not website and previous is None and deferred_error is None:
                        progress(f"Looking for the official website for {name}", completed, total)
                        try:
                            key = name.casefold()
                            if key not in websites:
                                websites[key] = discover_website(serper, search_id, name, spec.city)
                            website = websites[key]
                        except SearchError as exc:
                            deferred_error = exc
                    published, published_source = Contacts(), ""
                    if website and not (contact.mobile and contact.whatsapp and contact.email):
                        published, published_source = reader.contacts(website)
                    contacts = contact_fields([
                        dict(mobile=record.mobile, whatsapp=record.whatsapp, email=record.email,
                             source=link, whatsapp_source=link if record.whatsapp else "",
                             email_source=link if record.email else "")
                        for record, link in ((contact, url), (published, published_source))
                    ])
                    lead = dict(name=name, category=spec.category, city=spec.city, state=spec.state,
                                instagram=url if platform == "Instagram" else "",
                                facebook=url if platform == "Facebook" else "",
                                website=website, collected=now().date().isoformat(), **contacts)
                    duplicate = store.save(search_id, lead, platform, url, settings.daily_target)
                    if duplicate is None:
                        raise LimitReached("Daily profile target reached (India time).")
                    seen[url] = Contacts(lead["mobile"] or (previous.mobile if previous else ""),
                                         lead["whatsapp"] or (previous.whatsapp if previous else ""),
                                         lead["email"] or (previous.email if previous else ""))
                    matches += previous is None
                completed += 1
                if deferred_error:
                    raise deferred_error
                if not results:
                    # Empty pages end pagination for this query.
                    completed += spec.pages - page
                    break
    except LimitReached as exc:
        status = f"Stopped · {exc}"
    except SearchError as exc:
        status = f"Partial · {exc}"
    except Exception:
        status = "Failed · unexpected local error"
        raise
    finally:
        store.finish(search_id, status, matches)
    progress(status, total if status == "Completed" else completed, total)
    return search_id, status
