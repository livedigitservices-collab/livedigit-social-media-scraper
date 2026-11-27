import socket
from unittest.mock import patch

import pytest

from livedigit.discovery import Contacts, WebsiteReader, business_result, extract_contacts, normalize_mobile, profile_url


@pytest.mark.parametrize(("raw", "expected"), [
    ("+91 98765-43210", "+919876543210"),
    ("09876543210", "+919876543210"),
    ("919876543210", "+919876543210"),
    ("0401234567", ""),
    ("98765432101", ""),
    ("+44 9876543210", ""),
    ("abc 9876543210", ""),
    ("+91987654321", ""),
])
def test_normalize_mobile(raw, expected):
    assert normalize_mobile(raw) == expected


def test_only_labeled_phones_and_explicit_whatsapp():
    assert extract_contacts("Booking ref 9876543210").has_number is False
    assert extract_contacts("Phone: 040 98765432 | owner mobile: 9876543210") == Contacts(landline="+914098765432")
    assert extract_contacts("ఫోన్: +91 98765-43210 | WhatsApp: 8765432109 | info@example.org") == Contacts(
        "+919876543210", "+918765432109", "info@example.org",
    )
    assert extract_contacts("https://wa.me/919876543210").whatsapp == "+919876543210"
    assert extract_contacts("Phone: 9876543210").whatsapp == ""
    assert not extract_contacts("Phone: 9876543210 1").has_number


@pytest.mark.parametrize("url", [
    "https://instagram.com/reels/", "https://www.instagram.com/p/a/", "https://instagram.com/stories/cafe/",
    "https://instagram.com/cafe/extra", "https://instagram.com.evil.test/cafe/",
    "https://anyone@instagram.com/cafe/",
])
def test_non_profile_urls(url):
    assert not profile_url(url, "Instagram")


def test_business_location_filter_and_canonical_urls():
    item = {"link": "https://m.facebook.com/Local.Cafe/?ref=foo",
            "title": "Local Cafe | Facebook", "snippet": "A Hyderabad restaurant"}
    assert business_result(item, "Facebook", "Restaurants", "Hyderabad", "హైదరాబాద్", "")[:2] == (
        "Local Cafe", "https://www.facebook.com/local.cafe/",
    )
    assert business_result({**item, "snippet": "A Mumbai restaurant"}, "Facebook", "Restaurants",
                           "Hyderabad", "హైదరాబాద్", "") is None
    assert business_result({**item, "snippet": "Hyderabad influencer"}, "Facebook", "Restaurants",
                           "Hyderabad", "హైదరాబాద్", "") is None


class WebResponse:
    def __init__(self, text, content_type, status=200):
        self.status = status
        self.text = text
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def stream(self, _, *, decode_content):
        assert not decode_content
        yield self.text.encode()


class WebSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, address, *, port, server_hostname, assert_hostname, cert_reqs, ca_certs):
        assert address == "93.184.216.34" and port == 443
        assert server_hostname == assert_hostname == "cafe.example"
        assert cert_reqs == "CERT_REQUIRED" and ca_certs
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def urlopen(self, method, path, **kwargs):
        assert method == "GET" and not kwargs["redirect"] and not kwargs["retries"]
        url = "https://" + kwargs["headers"]["Host"] + path
        self.calls.append(url)
        return self.pages[url]


def test_authorized_website_reads_allowed_contact_page(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None,) * 4 + (("93.184.216.34", 443),)])
    root = "https://cafe.example"
    session = WebSession({
        root + "/robots.txt": WebResponse("User-agent: *\nDisallow: /about", "text/plain"),
        root + "/": WebResponse('<a href="/about">About</a><a href="/contact">Contact</a>', "text/html"),
        root + "/contact": WebResponse('<script>Phone: 8765432109</script><a href="tel:+919876543210">Call</a>', "text/html"),
    })
    assert WebsiteReader(set(), session).contacts(root) == (Contacts(), "")
    assert session.calls == []
    with patch("livedigit.discovery.time.sleep"):
        assert WebsiteReader({"cafe.example"}, session).contacts(root) == (Contacts(mobile="+919876543210"), root + "/contact")
    assert session.calls == [root + "/robots.txt", root + "/", root + "/contact"]


def test_website_denies_private_dns_and_redirect(monkeypatch):
    root = "https://cafe.example"
    session = WebSession({root + "/robots.txt": WebResponse("", "text/plain", 302)})
    reader = WebsiteReader({"cafe.example"}, session)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None,) * 4 + (("127.0.0.1", 443),)])
    assert reader.contacts(root) == (Contacts(), "")
    assert session.calls == []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None,) * 4 + (("93.184.216.34", 443),)])
    assert WebsiteReader({"cafe.example"}, session).contacts(root) == (Contacts(), "")
    assert session.calls == [root + "/robots.txt"]
