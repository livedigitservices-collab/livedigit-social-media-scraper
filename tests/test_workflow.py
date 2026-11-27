import csv
from io import BytesIO, StringIO
from pathlib import Path

from openpyxl import load_workbook
from streamlit.testing.v1 import AppTest

from livedigit.discovery import Contacts
from livedigit.export import csv_bytes, excel_bytes, table
from livedigit.search import SERPER_URL, Search, Serper, Settings, run_search
from livedigit.store import Store


class Response:
    def __init__(self, organic, status_code=200):
        self.organic = organic
        self.status_code = status_code

    def json(self):
        return {"organic": self.organic}


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, url, *, json, headers, timeout, allow_redirects):
        assert url == SERPER_URL
        assert headers["X-API-KEY"] == "mock-key"
        assert not allow_redirects
        self.calls.append(json)
        return next(self.responses)


def item(platform, slug, snippet, name="Jasmine Cafe"):
    return {"link": f"https://{platform.lower()}.com/{slug}/", "title": name, "snippet": snippet}


def test_paginated_search_dedupes_numbers_and_urls_and_persists(tmp_path):
    path = tmp_path / "leads.sqlite3"
    store = Store(path)
    settings = Settings("mock-key", interval=0)
    first = item("Instagram", "jasminecafe", "Hyderabad restaurant | Call: 9876543210")
    facebook = item("Facebook", "jasmine", "Hyderabad restaurant | WhatsApp: 9876543210")
    second = item("Instagram", "lotuscafe", "హైదరాబాద్ రెస్టారెంట్", "Lotus Cafe")
    session = Session(Response([first, first]), Response([]), Response([second]), Response([]),
                      Response([facebook]), Response([]), Response([]))
    progress = []
    spec = Search("Telangana", "Hyderabad", "Restaurants", ("Instagram", "Facebook"), ("English", "Telugu"), pages=2)
    search_id, status = run_search(spec, settings, store, lambda *args: progress.append(args),
                                   serper=Serper(settings, store, session))
    assert status == "Completed"
    assert progress[-1] == ("Completed", 8, 8)
    assert "హైదరాబాద్" in session.calls[2]["q"]
    assert [call["page"] for call in session.calls] == [1, 2, 1, 2, 1, 2, 1]
    store = Store(path)
    assert store.metrics() == (2, 1, 2, 1, 2, 7)
    assert store.today() == (3, 7)
    leads = store.rows(search_id, contacts_only=True)
    assert len(leads) == 1
    assert leads[0]["mobile"] == "+919876543210"
    assert leads[0]["facebook"] == "https://www.facebook.com/jasmine/"
    assert leads[0]["source"] == leads[0]["instagram"]
    assert leads[0]["whatsapp"] == "+919876543210"
    assert leads[0]["whatsapp_source"] == leads[0]["facebook"]
    assert "ownership unverified" in leads[0]["verification"]
    assert store.history()[0]["credits"] == 7
    assert store.history()[0]["matches"] == 3


def test_enrichment_and_credit_limit_preserve_partial_leads(tmp_path):
    store = Store(tmp_path / "leads.sqlite3")
    settings = Settings("mock-key", credit_limit=3, interval=0)
    session = Session(
        Response([item("Instagram", "jasmine", "Guntur restaurant"), item("Instagram", "lotus", "Guntur cafe", "Lotus Cafe")]),
        Response([]),
        Response([{"link": "https://jasminecafe.example", "title": "Jasmine Cafe", "snippet": "Guntur"}]),
    )

    class Reader:
        def contacts(self, url):
            assert url == "https://jasminecafe.example"
            return Contacts(whatsapp="+919876543210"), url + "/contact"

    spec = Search("Andhra Pradesh", "Guntur", "Restaurants", ("Instagram",), ("English",), pages=2, discover_websites=True)
    search_id, status = run_search(spec, settings, store, serper=Serper(settings, store, session), reader=Reader())
    assert status.startswith("Stopped")
    assert len(session.calls) == 3
    assert store.rows(search_id, contacts_only=True)[0]["source"] == "https://jasminecafe.example/contact"
    assert store.rows(search_id, contacts_only=True)[0]["whatsapp"] == "+919876543210"


def test_target_and_provider_error(tmp_path):
    for target, expected, pages in [(1, "Stopped", 1), (5, "Partial", 2)]:
        store = Store(tmp_path / f"test-{target}.sqlite3")
        settings = Settings("mock-key", daily_target=target, interval=0)
        results = [item("Instagram", slug, "Tirupati cafe", slug) for slug in ("jasmine", "lotus")]
        session = Session(Response(results), Response([], 429))
        spec = Search("Andhra Pradesh", "Tirupati", "Restaurants", ("Instagram",), ("English",), pages=2)
        search_id, status = run_search(spec, settings, store, serper=Serper(settings, store, session))
        assert status.startswith(expected)
        assert len(store.rows(search_id)) == (1 if target == 1 else 2)
        assert len(session.calls) == pages


def test_spreadsheet_exports_are_safe_and_preserve_unicode():
    frame = table([{"name": "=1+1", "city": "హైదరాబాద్", "mobile": "+919876543210"}])
    text = next(csv.DictReader(StringIO(csv_bytes(frame).decode("utf-8-sig"))))
    assert text["Business name"] == "'=1+1"
    assert text["City"] == "హైదరాబాద్"
    assert text["Public business mobile number"] == "'+919876543210"
    sheet = load_workbook(BytesIO(excel_bytes(frame))).active
    assert sheet["A2"].value == "=1+1" and sheet["A2"].data_type == "s"
    assert sheet["E2"].value == "+919876543210" and sheet["E2"].data_type == "s"


def test_streamlit_search_and_results(tmp_path, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "mock-key")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.sqlite3"))
    monkeypatch.setenv("SERPER_MIN_INTERVAL", "0.25")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    session = Session(Response([item("Instagram", "jasmine", "Visakhapatnam cafe | Phone: 9876543210")]))
    monkeypatch.setattr("requests.sessions.Session.post", lambda self, *args, **kwargs: session.post(*args, **kwargs))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
    assert not app.exception
    app.selectbox(key="search_mode").set_value("Instagram businesses")
    app.multiselect[0].set_value(["English"])
    app.slider[0].set_value(1)
    app.button[0].click().run()
    assert not app.exception
    assert len(session.calls) == 1
    assert app.metric[0].value == "1"
    assert any("Business name" in grid.value and grid.value.iloc[0]["Business name"] == "Jasmine Cafe"
               for grid in app.dataframe)
    assert app.get("download_button")[0].label == "Download Excel"


def test_optional_lookup_exhaustion_keeps_other_results_from_page(tmp_path):
    store = Store(tmp_path / "leads.sqlite3")
    settings = Settings("mock-key", credit_limit=1, interval=0)
    results = [item("Instagram", slug, "Nellore cafe", slug) for slug in ("jasmine", "lotus")]
    session = Session(Response(results))
    spec = Search("Andhra Pradesh", "Nellore", "Restaurants", ("Instagram",), ("English",), discover_websites=True)
    search_id, status = run_search(spec, settings, store, serper=Serper(settings, store, session))
    assert status.startswith("Stopped")
    assert len(session.calls) == 1
    assert {row["name"] for row in store.rows(search_id)} == {"jasmine", "lotus"}


def test_railway_requires_dashboard_password(tmp_path, monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "mock-environment")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.sqlite3"))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
    assert not app.exception
    assert "DASHBOARD_PASSWORD" in app.error[0].value
    assert not app.metric
