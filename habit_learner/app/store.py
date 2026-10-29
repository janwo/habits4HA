"""SQLite-Speicher für beobachtete Ereignisse."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,      -- Unix-Zeit (UTC)
    entity_id    TEXT    NOT NULL,
    domain       TEXT    NOT NULL,
    old_state    TEXT,
    new_state    TEXT,
    attributes   TEXT,                  -- JSON: brightness, color, ...
    origin       TEXT    NOT NULL,      -- manual | automation | device | system
    user_id      TEXT,
    context_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_origin ON events(origin, ts);
"""

# Nur diese Attribute sind für Gewohnheiten interessant; der Rest bläht nur auf.
KEEP_ATTRS = {
    "brightness", "color_temp_kelvin", "color_mode",
    "hs_color", "rgb_color", "effect", "source", "media_content_type",
}


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def add(self, *, ts, entity_id, old_state, new_state, attributes,
            origin, user_id, context_id):
        attrs = {k: v for k, v in (attributes or {}).items() if k in KEEP_ATTRS}
        self.db.execute(
            "INSERT INTO events (ts, entity_id, domain, old_state, new_state,"
            " attributes, origin, user_id, context_id)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, entity_id, entity_id.split(".", 1)[0], old_state, new_state,
             json.dumps(attrs) if attrs else None, origin, user_id, context_id),
        )
        self.db.commit()

    def purge(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        cur = self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount

    def stats(self) -> dict:
        rows = self.db.execute(
            "SELECT origin, COUNT(*) FROM events GROUP BY origin").fetchall()
        return dict(rows)
