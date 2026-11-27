"""Parse public search results and explicitly authorized business websites."""

import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import certifi
import urllib3

LOCATIONS = {
    "Andhra Pradesh": {
        "Visakhapatnam": "విశాఖపట్నం", "Vijayawada": "విజయవాడ",
        "Guntur": "గుంటూరు", "Tirupati": "తిరుపతి", "Kakinada": "కాకినాడ",
        "Rajahmundry": "రాజమండ్రి", "Nellore": "నెల్లూరు",
        "Kurnool": "కర్నూలు", "Anantapur": "అనంతపురం", "Ongole": "ఒంగోలు",
    },
    "Telangana": {
        "Hyderabad": "హైదరాబాద్", "Warangal": "వరంగల్",
        "Karimnagar": "కరీంనగర్", "Khammam": "ఖమ్మం",
        "Nizamabad": "నిజామాబాద్", "Secunderabad": "సికింద్రాబాద్",
    },
}
CATEGORIES = {
    "Restaurants": ("restaurant", "రెస్టారెంట్", "cafe", "bakery"),
    "Salons": ("salon", "సెలూన్", "beauty parlour", "spa"),
    "Retail": ("shop", "దుకాణం", "store", "boutique"),
    "Clinics": ("clinic", "క్లినిక్", "dental", "hospital"),
    "Fitness": ("gym", "జిమ్", "fitness"),
    "Home services": ("home services", "గృహ సేవలు", "plumber", "electrician", "appliance repair", "AC repair", "pest control", "home cleaning", "carpenter"),
    "Event services": ("event planner", "ఈవెంట్ ప్లానర్", "catering", "decorators"),
    "Other businesses": ("local business", "స్థానిక వ్యాపారం", "business services", "local shop"),
}
STATE_TELUGU = {"Andhra Pradesh": "ఆంధ్రప్రదేశ్", "Telangana": "తెలంగాణ"}
SOCIAL = {"Instagram": "instagram.com", "Facebook": "facebook.com"}
RESERVED = {
    "instagram.com": set("p reel reels stories explore accounts direct tv about developer legal web share".split()),
    "facebook.com": set("posts photos videos reels reel stories share watch groups events marketplace search login help business pages people profile.php permalink.php story.php photo.php plugins sharer.php".split()),
}
NON_BUSINESS = re.compile(
    r"\b(influencer|digital creator|content creator|fan page|personal blog|public figure)\b",
    re.I,
)
BUSINESS = re.compile(
    r"\b(business|official|shop|store|clinic|studio|branch|bookings?|orders?|services?)\b|వ్యాపారం|దుకాణం",
    re.I,
)
PHONE_PATTERN = r"(?<![\w+0-9])\(?\+?[0-9][0-9() .-]{7,23}[0-9](?![\w0-9]|[ .-]+[0-9])"
WHATSAPP = r"(?:whats\s*app|వాట్సాప్|వాట్సప్)"
CONTACT = r"(?:contact(?:\s+(?:us|number|no\.?))?|call(?:\s+us)?|phone|mobile|tel|bookings?|reservations?|orders?|enquiries|సంప్రదించండి|ఫోన్|మొబైల్)"
LABEL_JOIN = r"\s*[:=–—-]?\s*"
EMAIL = re.compile(r"[a-z0-9][a-z0-9._%+-]*@[a-z0-9.-]+\.[a-z]{2,}", re.I)
PERSONAL_CONTACT = re.compile(r"\b(?:personal|private|owner(?:'s)?|home)\s+(?:phone|mobile|number|contact|whatsapp|email)\b", re.I)


@dataclass(frozen=True)
class Contacts:
    mobile: str = ""
    whatsapp: str = ""
    email: str = ""
    landline: str = ""

    @property
    def has_number(self):
        return bool(self.mobile or self.whatsapp or self.landline)

    @property
    def present(self):
        return self.has_number or bool(self.email)


def phone_digits(value):
    if not re.fullmatch(r"[+0-9 ().-]+", value):
        return ""
    return re.sub(r"[ ().-]", "", value)


def normalize_mobile(value):
    """Return E.164 for a format-valid Indian mobile number."""
    match = re.fullmatch(r"(?:(?:\+?91|0))?([6-9][0-9]{9})", phone_digits(value))
    return "+91" + match[1] if match else ""


def normalize_landline(value):
    """Keep national-length fixed lines only when an Indian prefix is explicit."""
    match = re.fullmatch(r"(?:\+91(?:0)?|0)([1-5][0-9]{9})", phone_digits(value))
    return "+91" + match[1] if match else ""


def extract_contacts(value):
    text = " ".join(value.split())
    text = re.sub(PERSONAL_CONTACT.pattern + r"[^,;|•]{0,100}", " ", text, flags=re.I)
    action = rf"(?:{CONTACT}|{WHATSAPP})"
    combined = rf"{action}\s*(?:or|and|/|&|లేదా)\s*{action}"
    qualifier = r"(?:\s+(?:us|on|at|now|for (?:bookings?|orders?|reservations?)|number|no\.?)){0,3}"
    numbers = {"mobile": "", "whatsapp": "", "landline": ""}
    pattern = rf"(?<!\w)(?P<label>{combined}|{action})(?!\w){qualifier}{LABEL_JOIN}(?P<number>{PHONE_PATTERN})"
    for match in re.finditer(pattern, text, re.I):
        label, raw = match["label"], match["number"]
        if re.search(CONTACT, label, re.I):
            numbers["mobile"] = numbers["mobile"] or normalize_mobile(raw)
            numbers["landline"] = numbers["landline"] or normalize_landline(raw)
        if re.search(WHATSAPP, label, re.I):
            numbers["whatsapp"] = numbers["whatsapp"] or normalize_mobile(raw)
    if not numbers["whatsapp"]:
        match = re.search(r"(?<![\w./-])https://(?:wa\.me/|api\.whatsapp\.com/send\?phone=)(91[0-9]{10})(?![\w0-9])", text, re.I)
        if match:
            numbers["whatsapp"] = normalize_mobile(match[1])
    email = EMAIL.search(text)
    return Contacts(email=email[0].lower() if email else "", **numbers)


def profile_url(value, platform):
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").removeprefix("www.").removeprefix("m.")
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.port:
            return ""
    except ValueError:
        return ""
    if host != SOCIAL[platform]:
        return ""
    slug = parsed.path.strip("/").lower()
    if platform == "Facebook":
        if slug == "profile.php":
            ids = parse_qs(parsed.query).get("id", [])
            if len(ids) == 1 and re.fullmatch(r"[0-9]{5,24}", ids[0]):
                return f"https://www.facebook.com/profile.php?id={ids[0]}"
        page = re.fullmatch(r"(?:people|pages)/[a-z0-9_.-]+/([0-9]{5,24})", slug)
        if page:
            return f"https://www.facebook.com/profile.php?id={page[1]}"
        page = re.fullmatch(r"p/[a-z0-9_.-]+-([0-9]{5,24})", slug)
        if page:
            return f"https://www.facebook.com/profile.php?id={page[1]}"
        slug = slug.removeprefix("pg/")
    if not re.fullmatch(r"[a-z0-9_.-]{2,100}", slug) or slug in RESERVED[host]:
        return ""
    return f"https://www.{host}/{slug}/"


def hostname(url):
    if "\\" in url or any(ord(character) <= 32 or ord(character) == 127 for character in url):
        return ""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.port:
            return ""
        return parsed.hostname or ""
    except ValueError:
        return ""


def website_domain(url):
    host = hostname(url)
    if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", host)
            or any(host == domain or host.endswith("." + domain)
                   for domain in (*SOCIAL.values(), "youtube.com", "linkedin.com", "tiktok.com",
                                  "wa.me", "whatsapp.com", "google.com", "linktr.ee"))):
        return ""
    return host


def result_text(item, key):
    value = item.get(key)
    return value.strip() if isinstance(value, str) else ""


CITY_VARIANTS = {
    "Visakhapatnam": ("Vizag", "వైజాగ్"),
    "Rajahmundry": ("Rajamahendravaram", "రాజమహేంద్రవరం"),
    "Nellore": ("Sri Potti Sriramulu Nellore",),
}
TELUGU_TERMS = {
    "Restaurants": ("కేఫ్", "హోటల్"), "Salons": ("బ్యూటీ పార్లర్", "హెయిర్ సెలూన్"),
    "Retail": ("షాపు", "స్టోర్"), "Clinics": ("ఆసుపత్రి", "దంత వైద్యశాల"),
    "Fitness": ("ఫిట్‌నెస్ సెంటర్", "వ్యాయామశాల"),
    "Home services": ("ప్లంబర్", "ఎలక్ట్రీషియన్"),
    "Event services": ("కేటరింగ్", "డెకరేషన్"), "Other businesses": ("సేవలు", "దుకాణం"),
}
MAPS = "Google Maps"


def has_term(text, term):
    # Telugu combining marks otherwise cause word-boundary mismatches.
    return bool(term and re.search(rf"(?<![a-z0-9]){re.escape(term.casefold())}(?![a-z0-9])", text.casefold()))


def city_matches(value, city, local_city):
    return any(has_term(value, form) for form in (city, local_city, *CITY_VARIANTS.get(city, ())))


def business_key(name):
    return "".join(character for character in name.casefold() if character.isalnum()
                   or "\u0c00" <= character <= "\u0c7f")


def official_domain(url):
    return website_domain(url).removeprefix("www.")


@dataclass(frozen=True)
class Candidate:
    name: str
    url: str
    website: str
    contact: Contacts
    source: str
    location_confirmed: bool = True


def inspect_social(item, platform, category, city, local_city, custom):
    link = profile_url(result_text(item, "link"), platform)
    title = result_text(item, "title")
    snippet = result_text(item, "snippet")
    if not link:
        return None, "Not a business profile URL"
    if not title:
        return None, "Missing business name"
    if NON_BUSINESS.search(f"{title} {snippet}"):
        return None, "Creator or personal profile"
    name = re.split(r"\s+\(@|\s+[|·–—-]\s+(?:Instagram|Facebook)\b", title, maxsplit=1, flags=re.I)[0].strip()
    if not name:
        return None, "Missing business name"
    evidence = f"{title} {snippet}"
    # Separated words in a public profile handle can supply context absent from an abbreviated snippet.
    handle = urlsplit(link).path.strip("/")
    searchable = f"{evidence} {re.sub(r'[_.-]+', ' ', handle)}"
    terms = (*CATEGORIES[category], *TELUGU_TERMS[category], custom.strip())
    if not BUSINESS.search(evidence) and not any(
        has_term(searchable, term) or (term.isascii() and has_term(searchable, term + "s")) for term in terms if term
    ):
        return None, "Missing business category evidence"
    match = re.search(r"(?:\b(?:official\s+)?website|వెబ్‌సైట్)\s*[:–-]?\s*(https?://[^\s<>]+)", snippet, re.I)
    website = match[1].rstrip(".,;!)]") if match else ""
    return Candidate(name, link, website if website_domain(website) else "",
                     extract_contacts(evidence), link, city_matches(searchable, city, local_city)), ""


def business_result(item, platform, category, city, local_city, custom):
    candidate, _ = inspect_social(item, platform, category, city, local_city, custom)
    if candidate and candidate.location_confirmed:
        return candidate.name, candidate.url, candidate.website, candidate.contact
    return None


def inspect_place(item, city, local_city):
    name, address = result_text(item, "title"), result_text(item, "address")
    if not name or not address:
        return None, "Missing listing name or address"
    if not city_matches(address, city, local_city):
        return None, "Listing address outside requested locality or missing locality evidence"
    if NON_BUSINESS.search(f"{name} {result_text(item, 'category')}"):
        return None, "Creator or personal profile"
    cid = str(item.get("cid", ""))
    place_id = result_text(item, "placeId")
    query = {"api": 1, "query": f"{name}, {address}"}
    stable_place_id = bool(re.fullmatch(r"[A-Za-z0-9_-]{8,200}", place_id))
    stable_cid = bool(re.fullmatch(r"[0-9]{1,24}", cid))
    if stable_place_id:
        query["query_place_id"] = place_id
    if stable_cid:
        link = "https://www.google.com/maps?" + urlencode({"cid": cid})
    else:
        link = "https://www.google.com/maps/search/?" + urlencode(query)
    website = result_text(item, "website")
    phone = result_text(item, "phoneNumber")
    contact = (Contacts(mobile=normalize_mobile(phone), landline=normalize_landline(phone))
               if stable_cid or stable_place_id else Contacts())
    return Candidate(name, link, website if website_domain(website) else "", contact, link), ""


class BusinessHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.contact_links = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.hidden += 1
        if tag != "a" or self.hidden:
            return
        link = dict(attrs).get("href") or ""
        if link.startswith("tel:"):
            self.parts.append(f" Contact: {link[4:]} | ")
        elif link.startswith("mailto:"):
            self.parts.append(f" Email: {link[7:].split('?')[0]} | ")
        elif link.startswith(("https://wa.me/", "https://api.whatsapp.com/send?")):
            self.parts.append(f" {link} | ")
        elif re.search(r"(?:^|/)(?:contact(?:-us)?|about(?:-us)?)/?(?:\.html)?$", link, re.I):
            self.contact_links.append(link)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template"} and self.hidden:
            self.hidden -= 1
        if tag in {"p", "li", "div", "section", "footer"}:
            self.parts.append(" | ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class WebsiteReader:
    """Read only same-host HTTPS pages on explicitly authorized public domains."""

    AGENT = "LiveDigitLeadFinder"
    WEBSITE_ERRORS = (urllib3.exceptions.HTTPError, OSError, ValueError)

    def __init__(self, domains, pool_factory=urllib3.HTTPSConnectionPool):
        self.domains = frozenset(domains)
        self.pool_factory = pool_factory
        self._last_request = {}
        self._robots = {}
        self._contacts = {}

    def _read(self, url, host, types, delay=1):
        if not url.startswith("https://") or website_domain(url) != host:
            return None
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            return None
        wait = self._last_request.get(host, 0) + delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request[host] = time.monotonic()
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if path.startswith("//") or parsed.query:
            return None
        # Connect to the checked address without resolving the hostname again.
        # Certificate verification and SNI still use the authorized hostname.
        with self.pool_factory(
            addresses[0][4][0], port=443, server_hostname=host, assert_hostname=host,
            cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(),
        ) as pool:
            with pool.urlopen(
                "GET", path, headers={"Host": host, "User-Agent": self.AGENT, "Accept-Encoding": "identity"},
                redirect=False, retries=False, timeout=urllib3.Timeout(connect=3, read=5),
                preload_content=False,
            ) as response:
                if response.status != 200 or not response.headers.get("Content-Type", "").lower().startswith(types):
                    return None
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    return None
                content = bytearray()
                deadline = time.monotonic() + 10
                for chunk in response.stream(8192, decode_content=False):
                    content.extend(chunk)
                    if len(content) > 512_000 or time.monotonic() > deadline:
                        return None
                return content.decode("utf-8", errors="replace")

    def contacts(self, website):
        if website not in self._contacts:
            self._contacts[website] = self._read_contacts(website)
        return self._contacts[website]

    def _read_contacts(self, website):
        host = website_domain(website)
        if not host or host not in self.domains or not website.startswith("https://"):
            return Contacts(), ""
        try:
            if host not in self._robots:
                robots = self._read(f"https://{host}/robots.txt", host, ("text/plain",))
                if robots is None:
                    self._robots[host] = None
                else:
                    rules = RobotFileParser()
                    rules.parse(robots.splitlines())
                    self._robots[host] = rules
            rules = self._robots[host]
            if rules is None or not rules.can_fetch(self.AGENT, website):
                return Contacts(), ""
            rate = rules.request_rate(self.AGENT)
            delay = max(1, rules.crawl_delay(self.AGENT) or 0,
                        rate.seconds / rate.requests if rate and rate.requests else 0)
            if delay > 5:
                return Contacts(), ""
            home = self._page(website, host, delay)
            if home is None:
                return Contacts(), ""
            best = extract_contacts(" ".join(home.parts))
            if best.has_number:
                return best, website
            for href in dict.fromkeys(home.contact_links[:2]):
                url = urljoin(website, href)
                if website_domain(url) != host or not rules.can_fetch(self.AGENT, url):
                    continue
                page = self._page(url, host, delay)
                if page:
                    found = extract_contacts(" ".join(page.parts))
                    if found.has_number or (found.present and not best.present):
                        best, website = found, url
                    if best.has_number:
                        break
            return best, website if best.present else ""
        except self.WEBSITE_ERRORS:
            return Contacts(), ""

    def _page(self, url, host, delay):
        html = self._read(url, host, ("text/html", "application/xhtml+xml"), delay)
        if html is None:
            return None
        parser = BusinessHTML()
        parser.feed(html)
        return parser
