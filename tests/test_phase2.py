import sqlite3

from livedigit.discovery import MAPS, Contacts, extract_contacts, inspect_place, profile_url
from livedigit.search import MODES, PLACES_URL, SERPER_URL, Search, Serper, Settings, run_search
from livedigit.store import Store


class Reply:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status

    def json(self):
        return self.data


class Provider:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []

    def post(self, url, *, json, headers, timeout, allow_redirects):
        assert headers == {"X-API-KEY": "mock-key"}
        assert not allow_redirects
        self.calls.append((url, json))
        return next(self.replies)


def test_query_plan_and_credit_preview():
    spec = Search("Andhra Pradesh", "Visakhapatnam", "Restaurants", MODES["Combined business search"],
                  ("English", "Telugu"), pages=2, variants=3, discover_websites=True, lookup_limit=4)
    queries = list(spec.queries())
    assert len(queries) == 18 and len(set(queries)) == 18
    assert spec.estimated_requests() == 40
    assert any("cafe Vizag" in query for _, _, query in queries)
    assert any("కేఫ్" in query for _, _, query in queries)
    assert all("site:" not in query for source, _, query in queries if source == MAPS)


def test_numeric_facebook_and_contact_label_variants():
    first = profile_url("https://m.facebook.com/profile.php?ref=search&id=12345678", "Facebook")
    second = profile_url("https://www.facebook.com/people/Jasmine-Cafe/12345678/", "Facebook")
    assert first == second == "https://www.facebook.com/profile.php?id=12345678"
    assert not profile_url("https://www.facebook.com/profile.php?id=12345678&id=98765432", "Facebook")
    assert not profile_url("https://www.facebook.com/people/Jasmine/12345678/posts", "Facebook")
    assert extract_contacts("Call or WhatsApp for bookings: (+91) 98765 43210 | Phone: 040-2345 6789") == Contacts(
        mobile="+919876543210", whatsapp="+919876543210", landline="+914023456789",
    )
    assert not extract_contacts("Owner WhatsApp: +91 98765 43210 | booking reference 9876543210").has_number


def test_places_and_social_corroboration_keep_contact_sources(tmp_path):
    store = Store(tmp_path / "leads.sqlite3")
    settings = Settings("mock-key", interval=0)
    places = {"title": "Jasmine Cafe", "address": "Banjara Hills, Hyderabad, Telangana",
              "phoneNumber": "+91 (40) 2345 6789", "website": "https://jasmine.example/", "cid": "123456789"}
    social = {"title": "Jasmine Cafe | Instagram", "link": "https://instagram.com/jasmine",
              "snippet": "Cafe · Website: https://www.jasmine.example/ | WhatsApp: +91 98765 43210"}
    facebook = {"title": "Jasmine Cafe | Facebook", "link": "https://m.facebook.com/profile.php?id=12345678",
                "snippet": "Hyderabad cafe · Website: https://jasmine.example/"}
    unrelated = {**social, "link": "https://instagram.com/other", "snippet": "Cafe · Website: https://other.example/"}
    creator = {"title": "Creator Cafe", "link": "https://instagram.com/cafecreator", "snippet": "Hyderabad influencer"}
    provider = Provider(Reply({"places": [places, {**places, "address": "Pune, Maharashtra"}]}),
                        Reply({"organic": [social, unrelated, creator, "bad row"]}), Reply({"organic": [facebook]}))
    spec = Search("Telangana", "Hyderabad", "Restaurants", MODES["Combined business search"], ("English",))
    search_id, status = run_search(spec, settings, store, serper=Serper(settings, store, provider))
    assert status == "Completed"
    assert [entry[0] for entry in provider.calls] == [PLACES_URL, SERPER_URL, SERPER_URL]
    lead, = store.rows(search_id, contacts_only=True)
    assert lead["maps"] == lead["landline_source"] == "https://www.google.com/maps?cid=123456789"
    assert lead["landline"] == "+914023456789" and not lead["mobile"]
    assert lead["whatsapp"] == "+919876543210"
    assert lead["whatsapp_source"] == lead["instagram"] == "https://www.instagram.com/jasmine/"
    assert lead["facebook"].endswith("id=12345678")
    assert "ownership unverified" in lead["verification"]
    stats = store.diagnostics(search_id)
    assert stats["raw"] == 7 and stats["accepted"] == 3 and stats["api_errors"] == 0
    assert sum(value for key, value in stats.items() if key.startswith("Rejected:")) == 4
    assert stats["Rejected: Missing corroboration for requested locality"] == 1
    assert store.history()[0]["credits"] == 3


def test_places_number_requires_linkable_listing():
    item = {"title": "Jasmine Cafe", "address": "Hyderabad, Telangana", "phoneNumber": "09876543210"}
    place, _ = inspect_place(item, "Hyderabad", "హైదరాబాద్")
    assert place and not place.contact.has_number
    place, _ = inspect_place({**item, "placeId": "ChIJfixturePlace"}, "Hyderabad", "హైదరాబాద్")
    assert place.contact.mobile == "+919876543210" and not place.contact.whatsapp
    assert "query_place_id=ChIJfixturePlace" in place.source


def test_repeated_places_page_and_continuing_after_endpoint_error(tmp_path):
    settings = Settings("mock-key", interval=0)
    store = Store(tmp_path / "leads.sqlite3")
    listing = {"title": "Lotus Cafe", "address": "Hyderabad, Telangana", "cid": "123"}
    provider = Provider(Reply({"places": [{**listing, "position": 1}]}),
                        Reply({"places": [{**listing, "position": 11}]}),
                        Reply({}, 502), Reply({"organic": []}))
    spec = Search("Telangana", "Hyderabad", "Restaurants", MODES["Combined business search"], ("English",), pages=3)
    search_id, status = run_search(spec, settings, store, serper=Serper(settings, store, provider))
    assert status.startswith("Partial")
    assert [(url, payload["page"]) for url, payload in provider.calls] == [
        (PLACES_URL, 1), (PLACES_URL, 2), (SERPER_URL, 1), (SERPER_URL, 1),
    ]
    assert store.diagnostics(search_id) == {
        "raw": 2, "accepted": 1, "api_errors": 1, "Rejected: Repeated response page": 1,
    }


def test_phase_one_sqlite_is_migrated_in_place(tmp_path):
    path = tmp_path / "previous.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE leads (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, city TEXT NOT NULL,
                state TEXT NOT NULL, mobile TEXT NOT NULL, whatsapp TEXT NOT NULL, instagram TEXT NOT NULL,
                facebook TEXT NOT NULL, website TEXT NOT NULL, email TEXT NOT NULL, source TEXT NOT NULL,
                verification TEXT NOT NULL, collected TEXT NOT NULL, whatsapp_source TEXT NOT NULL,
                email_source TEXT NOT NULL
            );
            INSERT INTO leads VALUES (1,'Jasmine Cafe','Restaurants','Hyderabad','Telangana',
                '+919876543210','','https://www.instagram.com/jasmine/','','https://jasmine.example/',
                '','https://www.instagram.com/jasmine/','Mobile format valid','2026-11-25','','');
            CREATE TABLE searches (id INTEGER PRIMARY KEY, started TEXT NOT NULL, description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Running', matches INTEGER NOT NULL DEFAULT 0,
                duplicates INTEGER NOT NULL DEFAULT 0);
            INSERT INTO searches VALUES (1,'2026-11-25','Previous search','Completed',1,0);
        """)
    Store(path)
    store = Store(path)
    row, = store.rows(contacts_only=True)
    assert row["id"] == 1 and row["mobile"] == "+919876543210"
    assert row["source"] == row["instagram"]
    assert row["maps"] == row["landline"] == ""
    assert store.history()[0]["description"] == "Previous search"
    assert store.diagnostics(1) == {}


def test_distinct_places_can_share_a_business_phone(tmp_path):
    store = Store(tmp_path / "leads.sqlite3")
    settings = Settings("mock-key", interval=0)
    base = {"title": "Jasmine Cafe", "phoneNumber": "+91 40 2345 6789", "website": "https://jasmine.example"}
    provider = Provider(Reply({"places": [
        {**base, "address": "Banjara Hills, Hyderabad", "cid": "321"},
        {**base, "address": "Jubilee Hills, Hyderabad", "cid": "654"},
    ]}))
    spec = Search("Telangana", "Hyderabad", "Restaurants", (MAPS,), ("English",))
    search_id, status = run_search(spec, settings, store, serper=Serper(settings, store, provider))
    assert status == "Completed"
    assert len(store.rows(search_id, contacts_only=True)) == 2


def test_social_home_services_queries_and_handle_evidence():
    from livedigit.discovery import inspect_social

    spec = Search("Telangana", "Hyderabad", "Home services", MODES["Instagram + Facebook businesses"],
                  ("English",), variants=3)
    queries = list(spec.queries())
    assert len(queries) == 6
    for host in ("instagram.com", "facebook.com"):
        assert {(source_query.split()[1]) for _, _, source_query in queries if host in source_query} == {
            "plumber", "electrician", "appliance",
        }
    result, reason = inspect_social(
        {"title": "Ravi", "link": "https://instagram.com/ravi_plumber_hyderabad", "snippet": "Call for bookings"},
        "Instagram", "Home services", "Hyderabad", "హైదరాబాద్", "",
    )
    assert not reason and result.location_confirmed
    assert profile_url("https://www.facebook.com/p/Jasmine-Cafe-12345678/", "Facebook").endswith("id=12345678")


def test_bulk_search_stops_after_1000_unique_profiles(tmp_path):
    store = Store(tmp_path / "bulk.sqlite3")
    settings = Settings("mock-key", credit_limit=200, daily_target=2000, interval=0)

    class BulkProvider(Provider):
        def post(self, url, **kwargs):
            assert url == SERPER_URL
            query = kwargs["json"]["q"]
            domain = "facebook.com" if query.startswith("site:facebook.com") else "instagram.com"
            start = len(self.calls) * 10
            self.calls.append((url, kwargs["json"]))
            return Reply({"organic": [
                {"title": f"Plumber {index}", "link": f"https://{domain}/plumber{index}",
                 "snippet": "Hyderabad plumbing services"}
                for index in range(start, start + 10)
            ]})

    provider = BulkProvider()
    spec = Search("Telangana", "Hyderabad", "Home services", MODES["Instagram + Facebook businesses"],
                  ("English",), pages=10, variants=8)
    search_id, status = run_search(spec, settings, store, serper=Serper(settings, store, provider))
    assert "1,000 profiles reached" in status
    assert len(provider.calls) == 100
    assert store.diagnostics(search_id)["accepted"] == len(store.rows(search_id)) == 1000
