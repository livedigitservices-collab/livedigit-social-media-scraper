"""Local, persistent lead and API usage storage."""

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from livedigit.discovery import MAPS, business_key, official_domain

IST = ZoneInfo("Asia/Kolkata")
FIELDS = {
    "name": "Business name", "category": "Business category", "city": "City",
    "state": "State", "mobile": "Public business mobile number",
    "whatsapp": "Public WhatsApp business number", "instagram": "Instagram URL",
    "facebook": "Facebook URL", "website": "Website", "email": "Public business email",
    "source": "Contact source URL", "verification": "Verification status",
    "collected": "Collection date", "whatsapp_source": "WhatsApp source URL",
    "email_source": "Email source URL",
    "maps": "Google Maps listing URL", "landline": "Public business landline",
    "landline_source": "Landline source URL",
}

PHASE_TWO_COLUMNS = ("maps", "landline", "landline_source", "business_key", "website_domain")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL, category TEXT NOT NULL, city TEXT NOT NULL, state TEXT NOT NULL,
    mobile TEXT NOT NULL, whatsapp TEXT NOT NULL, instagram TEXT NOT NULL, facebook TEXT NOT NULL,
    website TEXT NOT NULL, email TEXT NOT NULL, source TEXT NOT NULL,
    verification TEXT NOT NULL, collected TEXT NOT NULL,
    whatsapp_source TEXT NOT NULL, email_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    url TEXT PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
    platform TEXT NOT NULL, day TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS profiles_day ON profiles(day);
CREATE TABLE IF NOT EXISTS numbers (
    number TEXT PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id)
);
CREATE TABLE IF NOT EXISTS business_numbers (
    number TEXT NOT NULL, lead_id INTEGER NOT NULL REFERENCES leads(id),
    PRIMARY KEY (number, lead_id)
);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY, started TEXT NOT NULL, description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Running', matches INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, search_id INTEGER REFERENCES searches(id),
    started REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_day ON requests(day);
CREATE TABLE IF NOT EXISTS search_leads (
    search_id INTEGER NOT NULL REFERENCES searches(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    PRIMARY KEY (search_id, lead_id)
);
CREATE INDEX IF NOT EXISTS search_leads_lead ON search_leads(lead_id);
CREATE TABLE IF NOT EXISTS search_diagnostics (
    search_id INTEGER NOT NULL REFERENCES searches(id),
    label TEXT NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY (search_id, label)
);
"""


def now():
    return datetime.now(IST)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        with self.connect(write=True) as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(leads)")}
            if any(column not in columns for column in PHASE_TWO_COLUMNS):
                for column in PHASE_TWO_COLUMNS:
                    if column not in columns:
                        db.execute(f"ALTER TABLE leads ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                db.executemany(
                    "UPDATE leads SET business_key=?, website_domain=? WHERE id=?",
                    [(business_key(row["name"]), official_domain(row["website"]), row["id"])
                     for row in db.execute("SELECT id,name,website FROM leads")],
                )
                db.execute("INSERT OR IGNORE INTO business_numbers SELECT number,lead_id FROM numbers")
            db.execute("CREATE INDEX IF NOT EXISTS leads_identity ON leads(state, city, business_key)")

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def start(self, description):
        with self.connect(write=True) as db:
            return db.execute("INSERT INTO searches(started, description) VALUES (?, ?)",
                              (now().isoformat(timespec="seconds"), description)).lastrowid

    def finish(self, search_id, status, matches, diagnostics=None):
        with self.connect(write=True) as db:
            db.execute("UPDATE searches SET status=?, matches=? WHERE id=?", (status, matches, search_id))
            if diagnostics is not None:
                db.executemany("INSERT INTO search_diagnostics VALUES (?, ?, ?)",
                               [(search_id, label, count) for label, count in diagnostics.items()])

    def diagnostics(self, search_id):
        with self.connect() as db:
            return dict(db.execute("SELECT label,count FROM search_diagnostics WHERE search_id=?", (search_id,)))

    @staticmethod
    def _listing_matches(db, lead, platform, identifiers):
        name, domain = business_key(lead["name"]), official_domain(lead["website"])
        if not name or not (domain or identifiers):
            return set()
        matches = set()
        for row in db.execute(
            """SELECT id,maps,instagram,facebook,website_domain,mobile,whatsapp,landline FROM leads
               WHERE city=? AND state=? AND business_key=?""",
            (lead["city"], lead["state"], name),
        ):
            cross_source = ((row["instagram"] or row["facebook"]) and row["maps"] in {"", lead["maps"]}
                            if platform == MAPS else row["maps"])
            if cross_source and ((domain and row["website_domain"] == domain)
                                 or identifiers.intersection((row["mobile"], row["whatsapp"], row["landline"]))):
                matches.add(row["id"])
        return matches if len(matches) == 1 else set()

    def corroborates_location(self, lead, platform):
        with self.connect() as db:
            return bool(self._listing_matches(db, lead, platform, lead_numbers(lead)))

    def count_duplicate(self, search_id):
        with self.connect(write=True) as db:
            db.execute("UPDATE searches SET duplicates=duplicates+1 WHERE id=?", (search_id,))

    def reserve_request(self, search_id, limit, target, interval):
        """Reserve the next request and return its throttle delay, or None at a cap."""
        with self.connect(write=True) as db:
            day = now().date().isoformat()
            count = db.execute("SELECT count(*) FROM requests WHERE day=?", (day,)).fetchone()[0]
            profiles = db.execute("SELECT count(*) FROM profiles WHERE day=?", (day,)).fetchone()[0]
            if count >= limit or profiles >= target:
                return None
            last = db.execute("SELECT max(started) FROM requests").fetchone()[0]
            current = time.time()
            slot = max(current, (last or 0) + interval)
            db.execute("INSERT INTO requests(day, search_id, started) VALUES (?, ?, ?)", (day, search_id, slot))
            return slot - current

    def save(self, search_id, lead, platform, url, target, *, count_duplicate=True):
        """Return True for a duplicate, False for a new lead, or None at the profile cap."""
        with self.connect(write=True) as db:
            identifiers = lead_numbers(lead)
            known = db.execute("SELECT lead_id FROM profiles WHERE url=?", (url,)).fetchone()
            day = now().date().isoformat()
            if not known and db.execute("SELECT count(*) FROM profiles WHERE day=?", (day,)).fetchone()[0] >= target:
                return None
            matches = {known[0]} if known else set()
            related = self._listing_matches(db, lead, platform, identifiers)
            matches.update(related)
            number_matches = set()
            for number in identifiers:
                for row in db.execute(
                    """SELECT l.id,l.maps FROM business_numbers n JOIN leads l ON l.id=n.lead_id
                       WHERE n.number=? AND l.state=? AND l.city=? AND l.business_key=?""",
                    (number, lead["state"], lead["city"], business_key(lead["name"])),
                ):
                    if platform != MAPS or row["maps"] in {"", url}:
                        number_matches.add(row["id"])
            # Shared numbers can identify multiple branches. Require a single matching business.
            if len(number_matches) == 1:
                matches.update(number_matches)
            if len(matches) > 1:
                mapped = {row["maps"] for key in matches
                          if (row := db.execute("SELECT maps FROM leads WHERE id=?", (key,)).fetchone())["maps"]}
                if len(mapped) > 1:
                    matches = {known[0]} if known else set()
            if matches:
                lead_id = min(matches)
                existing = [dict(db.execute("SELECT * FROM leads WHERE id=?", (key,)).fetchone())
                            for key in sorted(matches)]
                updates = contact_fields([*existing, lead])
                for field in ("website", "instagram", "facebook", "maps"):
                    updates[field] = next((row[field] for row in [*existing, lead] if row[field]), "")
                updates["website_domain"] = official_domain(updates["website"])
                for other in matches - {lead_id}:
                    db.execute("UPDATE profiles SET lead_id=? WHERE lead_id=?", (lead_id, other))
                    db.execute("UPDATE numbers SET lead_id=? WHERE lead_id=?", (lead_id, other))
                    db.execute("INSERT OR IGNORE INTO business_numbers SELECT number,? FROM business_numbers WHERE lead_id=?",
                               (lead_id, other))
                    db.execute("DELETE FROM business_numbers WHERE lead_id=?", (other,))
                    db.execute("INSERT OR IGNORE INTO search_leads SELECT search_id, ? FROM search_leads WHERE lead_id=?",
                               (lead_id, other))
                    db.execute("DELETE FROM search_leads WHERE lead_id=?", (other,))
                    db.execute("DELETE FROM leads WHERE id=?", (other,))
                db.execute(
                    "UPDATE leads SET " + ", ".join(f"{field}=?" for field in updates) + " WHERE id=?",
                    (*updates.values(), lead_id),
                )
            else:
                columns = ", ".join((*FIELDS, "business_key", "website_domain"))
                values = (*[lead[field] for field in FIELDS], business_key(lead["name"]), official_domain(lead["website"]))
                placeholders = ", ".join("?" for _ in values)
                lead_id = db.execute(f"INSERT INTO leads({columns}) VALUES ({placeholders})", values).lastrowid
            db.execute("INSERT OR IGNORE INTO profiles VALUES (?, ?, ?, ?)", (url, lead_id, platform, day))
            db.executemany("INSERT OR IGNORE INTO business_numbers VALUES (?, ?)", [(number, lead_id) for number in identifiers])
            db.execute("INSERT OR IGNORE INTO search_leads VALUES (?, ?)", (search_id, lead_id))
            if matches and count_duplicate:
                db.execute("UPDATE searches SET duplicates=duplicates+? WHERE id=?", (max(1, len(matches) - 1), search_id))
            return bool(matches)

    def rows(self, search_id=None, contacts_only=False):
        sql = "SELECT l.* FROM leads l WHERE 1"
        args = []
        if search_id is not None:
            sql += " AND EXISTS (SELECT 1 FROM search_leads s WHERE s.lead_id=l.id AND s.search_id=?)"
            args.append(search_id)
        if contacts_only:
            sql += " AND (l.mobile<>'' OR l.whatsapp<>'' OR l.landline<>'')"
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql + " ORDER BY collected DESC, id DESC", args)]

    def metrics(self):
        with self.connect() as db:
            return (
                *db.execute("SELECT count(*), count(CASE WHEN mobile<>'' OR whatsapp<>'' OR landline<>'' THEN 1 END) FROM leads").fetchone(),
                *db.execute("""SELECT count(CASE WHEN platform='Instagram' THEN 1 END),
                                      count(CASE WHEN platform='Facebook' THEN 1 END) FROM profiles""").fetchone(),
                db.execute("SELECT coalesce(sum(duplicates), 0) FROM searches").fetchone()[0],
                db.execute("SELECT count(*) FROM requests").fetchone()[0],
            )

    def today(self):
        day = now().date().isoformat()
        with self.connect() as db:
            return (db.execute("SELECT count(*) FROM profiles WHERE day=?", (day,)).fetchone()[0],
                    db.execute("SELECT count(*) FROM requests WHERE day=?", (day,)).fetchone()[0])

    def history(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT s.*, (SELECT count(*) FROM requests r WHERE r.search_id=s.id) AS credits
                FROM searches s ORDER BY s.id DESC LIMIT 100
            """)]


def lead_numbers(lead):
    return {lead["mobile"], lead["whatsapp"], lead.get("landline", "")} - {""}


def contact_fields(rows):
    """Keep the first published value for each field and its original source."""
    updates = {}
    for field, source_field in (
        ("mobile", "source"), ("whatsapp", "whatsapp_source"), ("email", "email_source"), ("landline", "landline_source"),
    ):
        row = next((row for row in rows if row.get(field)), None)
        updates[field] = row[field] if row else ""
        updates[source_field] = (row[source_field] or row["source"]) if row else ""
    updates["source"] = updates["source"] or updates["landline_source"] or updates["whatsapp_source"] or updates["email_source"]
    if updates["mobile"] or updates["whatsapp"]:
        updates["verification"] = "Mobile format valid · ownership unverified"
    elif updates["landline"]:
        updates["verification"] = "Landline format valid · ownership unverified"
    elif updates["email"]:
        updates["verification"] = "Published email · ownership unverified"
    else:
        updates["verification"] = "No public contact found"
    return updates
