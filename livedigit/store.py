"""Local, persistent lead and API usage storage."""

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
FIELDS = {
    "name": "Business name", "category": "Business category", "city": "City",
    "state": "State", "mobile": "Public business mobile number",
    "whatsapp": "Public WhatsApp business number", "instagram": "Instagram URL",
    "facebook": "Facebook URL", "website": "Website", "email": "Public business email",
    "source": "Contact source URL", "verification": "Verification status",
    "collected": "Collection date", "whatsapp_source": "WhatsApp source URL",
    "email_source": "Email source URL",
}

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

    def finish(self, search_id, status, matches):
        with self.connect(write=True) as db:
            db.execute("UPDATE searches SET status=?, matches=? WHERE id=?",
                       (status, matches, search_id))

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

    def save(self, search_id, lead, platform, url, target):
        """Return True for a duplicate, False for a new lead, or None at the profile cap."""
        with self.connect(write=True) as db:
            identifiers = {lead["mobile"], lead["whatsapp"]} - {""}
            known = db.execute("SELECT lead_id FROM profiles WHERE url=?", (url,)).fetchone()
            day = now().date().isoformat()
            if not known and db.execute("SELECT count(*) FROM profiles WHERE day=?", (day,)).fetchone()[0] >= target:
                return None
            matches = {known[0]} if known else set()
            for number in identifiers:
                match = db.execute("SELECT lead_id FROM numbers WHERE number=?", (number,)).fetchone()
                if match:
                    matches.add(match[0])
            if matches:
                lead_id = min(matches)
                existing = [dict(db.execute("SELECT * FROM leads WHERE id=?", (key,)).fetchone())
                            for key in sorted(matches)]
                updates = contact_fields([*existing, lead])
                for field in ("website", "instagram", "facebook"):
                    updates[field] = next((row[field] for row in [*existing, lead] if row[field]), "")
                for other in matches - {lead_id}:
                    db.execute("UPDATE profiles SET lead_id=? WHERE lead_id=?", (lead_id, other))
                    db.execute("UPDATE numbers SET lead_id=? WHERE lead_id=?", (lead_id, other))
                    db.execute("INSERT OR IGNORE INTO search_leads SELECT search_id, ? FROM search_leads WHERE lead_id=?",
                               (lead_id, other))
                    db.execute("DELETE FROM search_leads WHERE lead_id=?", (other,))
                    db.execute("DELETE FROM leads WHERE id=?", (other,))
                db.execute(
                    "UPDATE leads SET " + ", ".join(f"{field}=?" for field in updates) + " WHERE id=?",
                    (*updates.values(), lead_id),
                )
            else:
                columns = ", ".join(FIELDS)
                placeholders = ", ".join("?" for _ in FIELDS)
                lead_id = db.execute(f"INSERT INTO leads({columns}) VALUES ({placeholders})",
                                     tuple(lead[field] for field in FIELDS)).lastrowid
            db.execute("INSERT OR IGNORE INTO profiles VALUES (?, ?, ?, ?)", (url, lead_id, platform, day))
            db.executemany("INSERT OR IGNORE INTO numbers VALUES (?, ?)", [(number, lead_id) for number in identifiers])
            db.execute("INSERT OR IGNORE INTO search_leads VALUES (?, ?)", (search_id, lead_id))
            if matches:
                db.execute("UPDATE searches SET duplicates=duplicates+? WHERE id=?", (max(1, len(matches) - 1), search_id))
            return bool(matches)

    def rows(self, search_id=None, contacts_only=False):
        sql = "SELECT l.* FROM leads l WHERE 1"
        args = []
        if search_id is not None:
            sql += " AND EXISTS (SELECT 1 FROM search_leads s WHERE s.lead_id=l.id AND s.search_id=?)"
            args.append(search_id)
        if contacts_only:
            sql += " AND (l.mobile<>'' OR l.whatsapp<>'')"
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql + " ORDER BY collected DESC, id DESC", args)]

    def metrics(self):
        with self.connect() as db:
            return (
                *db.execute("SELECT count(*), count(CASE WHEN mobile<>'' OR whatsapp<>'' THEN 1 END) FROM leads").fetchone(),
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


def contact_fields(rows):
    """Keep the first published value for each field and its original source."""
    updates = {}
    for field, source_field in (("mobile", "source"), ("whatsapp", "whatsapp_source"), ("email", "email_source")):
        row = next((row for row in rows if row[field]), None)
        updates[field] = row[field] if row else ""
        updates[source_field] = (row[source_field] or row["source"]) if row else ""
    updates["source"] = updates["source"] or updates["whatsapp_source"] or updates["email_source"]
    if updates["mobile"] or updates["whatsapp"]:
        updates["verification"] = "Mobile format valid · ownership unverified"
    elif updates["email"]:
        updates["verification"] = "Published email · ownership unverified"
    else:
        updates["verification"] = "No public contact found"
    return updates
