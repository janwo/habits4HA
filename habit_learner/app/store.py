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
    context_id   TEXT,
    kind         TEXT    NOT NULL DEFAULT 'action',  -- action | trigger
    area_id      TEXT,
    automation   TEXT,                  -- auslösende automation.* / script.*
    context      TEXT                   -- JSON: Sonne, Anwesenheit, Medien
);
CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_origin ON events(origin, ts);

-- Schattenmodus: Vorhersagen und was tatsächlich passiert ist
CREATE TABLE IF NOT EXISTS predictions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern    TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    expected   TEXT NOT NULL,
    opened     REAL NOT NULL,
    closes     REAL NOT NULL,
    result     TEXT,                  -- treffer | abweichend | verpasst
    resolved   REAL,
    delay_min  REAL                   -- Abstand zur erwarteten Mitte
);
CREATE INDEX IF NOT EXISTS idx_predictions_pattern ON predictions(pattern, opened);

-- Vorschläge an Menschen und ihre Antworten
CREATE TABLE IF NOT EXISTS suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern     TEXT NOT NULL,
    text        TEXT NOT NULL,
    config      TEXT NOT NULL,           -- JSON: Automation, die angelegt würde
    offered     REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'offen',  -- offen | angenommen | abgelehnt | später
    answered    REAL,
    automation  TEXT                     -- ID der angelegten Automation
);
CREATE INDEX IF NOT EXISTS idx_suggestions_pattern ON suggestions(pattern, offered);
"""

# Spalten, die nach v0.1 dazukamen – bestehende Datenbanken werden ergänzt.
MIGRATIONS = {
    "kind": "TEXT NOT NULL DEFAULT 'action'",
    "area_id": "TEXT",
    "automation": "TEXT",
    "context": "TEXT",
}

# Nur diese Attribute sind für Gewohnheiten interessant; der Rest bläht nur auf.
KEEP_ATTRS = {
    "brightness", "color_temp_kelvin", "color_mode",
    "hs_color", "rgb_color", "effect", "source", "media_content_type",
}


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
        for col, decl in MIGRATIONS.items():
            if col not in cols:
                self.db.execute(f"ALTER TABLE events ADD COLUMN {col} {decl}")
        self.db.commit()

    def add(self, *, ts, entity_id, old_state, new_state, attributes,
            origin, user_id, context_id, kind="action", area_id=None,
            automation=None, context=None):
        attrs = {k: v for k, v in (attributes or {}).items() if k in KEEP_ATTRS}
        self.db.execute(
            "INSERT INTO events (ts, entity_id, domain, old_state, new_state,"
            " attributes, origin, user_id, context_id, kind, area_id,"
            " automation, context)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, entity_id, entity_id.split(".", 1)[0], old_state, new_state,
             json.dumps(attrs) if attrs else None, origin, user_id, context_id,
             kind, area_id, automation, json.dumps(context) if context else None),
        )
        self.db.commit()

    def add_prediction(self, *, pattern, entity_id, expected, opened, closes) -> int:
        cur = self.db.execute(
            "INSERT INTO predictions (pattern, entity_id, expected, opened, closes)"
            " VALUES (?,?,?,?,?)", (pattern, entity_id, expected, opened, closes))
        self.db.commit()
        return cur.lastrowid

    def resolve_prediction(self, pred_id, *, result, resolved, delay):
        self.db.execute(
            "UPDATE predictions SET result = ?, resolved = ?, delay_min = ? WHERE id = ?",
            (result, resolved, delay, pred_id))
        self.db.commit()

    def close_stale_predictions(self, now: float):
        """Nach einem Neustart offene, abgelaufene Vorhersagen nicht werten."""
        self.db.execute("DELETE FROM predictions WHERE result IS NULL AND closes < ?", (now,))
        self.db.commit()

    def prediction_stats(self, days: int) -> list[tuple[str, int, int]]:
        cutoff = time.time() - days * 86400
        return self.db.execute(
            "SELECT pattern, SUM(result = 'treffer'), COUNT(*) FROM predictions"
            " WHERE result IS NOT NULL AND opened >= ? GROUP BY pattern"
            " ORDER BY pattern", (cutoff,)).fetchall()

    def add_suggestion(self, *, pattern, text, config, offered) -> int:
        cur = self.db.execute(
            "INSERT INTO suggestions (pattern, text, config, offered) VALUES (?,?,?,?)",
            (pattern, text, json.dumps(config), offered))
        self.db.commit()
        return cur.lastrowid

    def answer_suggestion(self, sid, *, status, answered, automation=None):
        self.db.execute(
            "UPDATE suggestions SET status = ?, answered = ?, automation = ? WHERE id = ?",
            (status, answered, automation, sid))
        self.db.commit()

    def suggestion(self, sid) -> dict | None:
        row = self.db.execute(
            "SELECT pattern, text, config, status FROM suggestions WHERE id = ?",
            (sid,)).fetchone()
        if not row:
            return None
        return {"pattern": row[0], "text": row[1], "config": json.loads(row[2]),
                "status": row[3]}

    def last_suggestion(self, pattern) -> tuple[str, float] | None:
        """Status und Zeitpunkt (Antwort, sonst Angebot) des jüngsten Vorschlags."""
        return self.db.execute(
            "SELECT status, COALESCE(answered, offered) FROM suggestions"
            " WHERE pattern = ? ORDER BY id DESC LIMIT 1", (pattern,)).fetchone()

    def suggestions(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, pattern, text, offered, status, answered, automation"
            " FROM suggestions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        keys = ("id", "pattern", "text", "offered", "status", "answered", "automation")
        return [dict(zip(keys, row)) for row in rows]

    def offers_since(self, ts: float) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM suggestions WHERE offered >= ?", (ts,)).fetchone()[0]

    def purge(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        self.db.execute("DELETE FROM predictions WHERE opened < ?", (cutoff,))
        cur = self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount

    def stats(self) -> dict:
        rows = self.db.execute(
            "SELECT kind || ':' || origin, COUNT(*) FROM events"
            " GROUP BY kind, origin").fetchall()
        return dict(rows)
