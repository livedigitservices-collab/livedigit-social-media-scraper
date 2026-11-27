"""Search orchestration with persistent quotas and bounded external requests."""

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass

import requests

from livedigit.discovery import (
    CATEGORIES, CITY_VARIANTS, LOCATIONS, MAPS, SOCIAL, STATE_TELUGU, TELUGU_TERMS,
    Contacts, WebsiteReader, business_key, city_matches, inspect_place, inspect_social,
    result_text, website_domain,
)
from livedigit.store import contact_fields, now

SERPER_URL = "https://google.serper.dev/search"
PLACES_URL = "https://google.serper.dev/places"
MODES = {
    "Instagram businesses": ("Instagram",),
    "Facebook businesses": ("Facebook",),
    "Google Maps businesses": (MAPS,),
    "Combined business search": (MAPS, "Instagram", "Facebook"),
    "Instagram + Facebook businesses": ("Instagram", "Facebook"),
}
LANGUAGES = ("English", "Telugu")


class SearchError(Exception):
    pass


class LimitReached(SearchError):
    pass


class ProviderError(SearchError):
    def __init__(self, message, stop=False):
        super().__init__(message)
        self.stop = stop


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
    variants: int = 1
    lookup_limit: int = 5
    profile_limit: int = 1000

    @property
    def local_city(self):
        return LOCATIONS[self.state].get(self.city, self.city)

    def queries(self):
        keywords = CATEGORIES[self.category]
        emitted = set()
        for variant in range(self.variants):
            for platform in self.platforms:
                for language in self.languages:
                    if language == "Telugu":
                        terms = (keywords[1], *TELUGU_TERMS[self.category])
                        locations = (self.local_city, self.city,
                                     *CITY_VARIANTS.get(self.city, ()))
                        state = STATE_TELUGU[self.state]
                    else:
                        terms = (keywords[0], *keywords[2:])
                        locations = (self.city, *(alias for alias in CITY_VARIANTS.get(self.city, ()) if alias.isascii()))
                        if platform in SOCIAL and self.category == "Home services":
                            terms = (*keywords[2:], keywords[0])
                        state = self.state
                    city = locations[variant % len(locations)]
                    if platform in SOCIAL and variant >= len(terms) and len(locations) == 1:
                        # A shorter metro query is a useful second pass when business snippets omit the state.
                        state = ""
                    query = " ".join(part for part in (terms[variant % len(terms)], city, state, self.keyword) if part)
                    if platform in SOCIAL:
                        query = f"site:{SOCIAL[platform]} {query}"
                    row = platform, language, query
                    if row not in emitted:
                        emitted.add(row)
                        yield row

    def estimated_requests(self):
        return len(list(self.queries())) * self.pages + (self.lookup_limit if self.discover_websites else 0)


class Serper:
    def __init__(self, settings, store, session=None):
        self.settings = settings
        self.store = store
        self.session = session or requests.Session()

    def search(self, search_id, query, language, page, *, places=False):
        endpoint, field = (PLACES_URL, "places") if places else (SERPER_URL, "organic")
        delay = self.store.reserve_request(
            search_id, self.settings.credit_limit, self.settings.daily_target, self.settings.interval,
        )
        if delay is None:
            raise LimitReached("Daily profile target or API request limit reached (India time).")
        if delay:
            time.sleep(delay)
        try:
            response = self.session.post(
                endpoint, json={"q": query, "gl": "in", "hl": "te" if language == "Telugu" else "en", "page": page},
                headers={"X-API-KEY": self.settings.key}, timeout=(5, 15), allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ProviderError("Serper could not be reached. Check network access and try again later.", stop=True) from exc
        if response.status_code in {401, 403}:
            raise ProviderError("Serper refused authentication. Check SERPER_API_KEY and network permissions.", stop=True)
        if response.status_code in {402, 429}:
            raise ProviderError("Serper credit or rate limit reached. Check your account before trying again.", stop=True)
        if response.status_code != 200:
            raise ProviderError(f"Serper returned HTTP {response.status_code}.")
        try:
            data = response.json()
            results = data.get(field, []) if isinstance(data, dict) else None
            if not isinstance(results, list) or (isinstance(data, dict) and data.get("message") and field not in data):
                raise ValueError
        except ValueError as exc:
            raise ProviderError("Serper returned an unexpected response.") from exc
        return results


def discover_website(serper, search_id, name, city, local_city):
    """Use a website URL only when search evidence ties the domain to the business and city."""
    # Remove search operators from provider-supplied names before quoting.
    clean_name = re.sub(r'[^\w\s&.-]', " ", name).strip()[:100]
    if len(clean_name) < 3:
        return ""
    results = serper.search(search_id, f'"{clean_name}" {city} official website', "English", 1)
    excluded = {"justdial.com", "indiamart.com", "sulekha.com", "zomato.com", "swiggy.com",
                "tripadvisor.in", "tripadvisor.com", "magicpin.in", "practo.com"}
    for item in results:
        if not isinstance(item, dict):
            continue
        url = result_text(item, "link")
        host = website_domain(url)
        if not host or any(host == domain or host.endswith("." + domain) for domain in excluded):
            continue
        title = result_text(item, "title").casefold()
        snippet = result_text(item, "snippet").casefold()
        compact_name = re.sub(r"[^\w]", "", clean_name.casefold())
        compact_host = re.sub(r"[^\w]", "", host.removeprefix("www.").rsplit(".", 1)[0])
        if (compact_name and compact_name in compact_host and clean_name.casefold() in title
                and city_matches(f"{title} {snippet}", city, local_city)):
            return url
    return ""


def contact_row(record, link):
    return dict(mobile=record.mobile, whatsapp=record.whatsapp, email=record.email,
                landline=record.landline, source=link, landline_source=link,
                whatsapp_source=link, email_source=link)


def run_search(spec, settings, store, progress=lambda message, done, total: None, serper=None, reader=None):
    if not settings.key:
        raise SearchError("Set SERPER_API_KEY before running a search.")
    if (spec.state not in LOCATIONS or spec.category not in CATEGORIES or not spec.city.strip()
            or not spec.platforms or any(p not in (*SOCIAL, MAPS) for p in spec.platforms)
            or not spec.languages or any(lang not in LANGUAGES for lang in spec.languages)
            or not 1 <= spec.pages <= 10 or not 1 <= spec.variants <= 8 or not 0 <= spec.lookup_limit <= 20
            or not 1 <= spec.profile_limit <= 1000):
        raise SearchError("Choose a valid mode, category, city, language and page budget.")
    serper = serper or Serper(settings, store)
    reader = reader or WebsiteReader(settings.domains)
    queries = list(spec.queries())
    total = len(queries) * spec.pages
    description = " · ".join((spec.city, spec.state, spec.category, ", ".join(spec.platforms),
                              ", ".join(spec.languages), f"{spec.pages} page(s)", spec.keyword or "Default keywords"))
    search_id = store.start(description)
    diagnostics = Counter(raw=0, api_errors=0)
    accepted = set()
    pending = []
    enrichable = {}
    remaining_lookups = spec.lookup_limit if spec.discover_websites else 0
    status = "Completed"
    completed = 0
    provider_notice = ""
    stop = None

    def reject(reason, count=1):
        diagnostics["Rejected: " + reason] += count

    def save(lead, candidate, platform, *, enrichment=False):
        published, published_source = Contacts(), ""
        if lead["website"] and not all((candidate.contact.mobile, candidate.contact.whatsapp,
                                       candidate.contact.email, candidate.contact.landline)):
            published, published_source = reader.contacts(lead["website"])
        lead.update(contact_fields([contact_row(candidate.contact, candidate.source),
                                    contact_row(published, published_source)]))
        return store.save(search_id, lead, platform, candidate.url, settings.daily_target,
                          count_duplicate=not enrichment)

    def process(candidate, platform):
        nonlocal stop
        lead = dict(name=candidate.name, category=spec.category, city=spec.city, state=spec.state,
                    instagram=candidate.url if platform == "Instagram" else "",
                    facebook=candidate.url if platform == "Facebook" else "",
                    maps=candidate.url if platform == MAPS else "", website=candidate.website,
                    collected=now().date().isoformat())
        if not candidate.location_confirmed and not store.corroborates_location(
            {**lead, **contact_row(candidate.contact, candidate.source)}, platform
        ):
            return False
        if len(accepted) >= spec.profile_limit and candidate.url not in accepted:
            reject("Per-search profile target reached")
            stop = stop or LimitReached(f"Per-search target of {spec.profile_limit:,} profiles reached.")
            return True
        duplicate = save(lead, candidate, platform)
        if duplicate is None:
            reject("Daily new-profile cap reached")
            stop = stop or LimitReached("Daily profile target reached (India time).")
        elif candidate.url in accepted:
            reject("Repeated profile or listing URL")
        else:
            accepted.add(candidate.url)
            if len(accepted) == spec.profile_limit:
                stop = stop or LimitReached(f"Per-search target of {spec.profile_limit:,} profiles reached.")
        if duplicate is not None and not candidate.website:
            enrichable[candidate.url] = (lead, candidate, platform)
        else:
            enrichable.pop(candidate.url, None)
        return True

    def enrich_missing_websites():
        nonlocal remaining_lookups, provider_notice
        websites = {}
        # Let discovery pages use the budget first, then prioritize leads without a number.
        for lead, candidate, platform in sorted(enrichable.values(), key=lambda item: item[1].contact.has_number):
            key = business_key(candidate.name)
            if key not in websites:
                if not remaining_lookups:
                    continue
                remaining_lookups -= 1
                progress(f"Looking for the official website for {candidate.name}", completed, total)
                try:
                    websites[key] = discover_website(serper, search_id, candidate.name, spec.city, spec.local_city)
                except ProviderError as exc:
                    diagnostics["api_errors"] += 1
                    provider_notice = str(exc)
                    if exc.stop:
                        raise
                    websites[key] = ""
            if websites[key]:
                save({**lead, "website": websites[key]}, candidate, platform, enrichment=True)

    try:
        for platform, language, query in queries:
            seen_pages = set()
            for page in range(1, spec.pages + 1):
                progress(f"Searching {platform} in {language}, page {page}", completed, total)
                try:
                    if platform == MAPS:
                        results = serper.search(search_id, query, language, page, places=True)
                    else:
                        results = serper.search(search_id, query, language, page)
                except ProviderError as exc:
                    diagnostics["api_errors"] += 1
                    provider_notice = str(exc)
                    if exc.stop:
                        raise
                    completed += spec.pages - page + 1
                    break
                diagnostics["raw"] += len(results)
                # Some responses repeat an earlier page with different positions or ordering.
                signature = tuple(sorted(json.dumps({k: v for k, v in item.items() if k != "position"}
                                                       if isinstance(item, dict) else item, sort_keys=True)
                                         for item in results))
                if not results or signature in seen_pages:
                    if results:
                        reject("Repeated response page", len(results))
                    completed += spec.pages - page + 1
                    break
                seen_pages.add(signature)
                for item in results:
                    if not isinstance(item, dict):
                        reject("Malformed result")
                        continue
                    if platform == MAPS:
                        candidate, reason = inspect_place(item, spec.city, spec.local_city)
                    else:
                        candidate, reason = inspect_social(item, platform, spec.category, spec.city, spec.local_city, spec.keyword)
                    if candidate is None:
                        reject(reason)
                    elif not process(candidate, platform):
                        pending.append((candidate, platform))
                completed += 1
                if stop:
                    raise stop
        for candidate, platform in pending:
            if not process(candidate, platform):
                reject("Missing corroboration for requested locality")
        pending.clear()
        if remaining_lookups and not stop:
            enrich_missing_websites()
    except LimitReached as exc:
        status = f"Stopped · {exc}"
    except ProviderError as exc:
        status = f"Partial · {exc}"
    except Exception:
        status = "Failed · unexpected local error"
        raise
    finally:
        # A later Places result can corroborate an otherwise sparse social snippet.
        try:
            for candidate, platform in pending:
                if not process(candidate, platform):
                    reject("Missing corroboration for requested locality")
        finally:
            if provider_notice and status == "Completed":
                status = f"Partial · {provider_notice}"
            diagnostics["accepted"] = len(accepted)
            store.finish(search_id, status, len(accepted), diagnostics)
    progress(status, total if status == "Completed" else completed, total)
    return search_id, status
