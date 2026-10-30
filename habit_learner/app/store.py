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
                                                -- | pausiert | gelöscht
    answered    REAL,
    automation  TEXT,                    -- ID der angelegten Automation
    since       REAL,                    -- ab hier wird die Automation gemessen
    paused      REAL                     -- pausiert seit
);
CREATE INDEX IF NOT EXISTS idx_suggestions_pattern ON suggestions(pattern, offered);

-- Autopilot: freigegebene Muster und was er geschaltet hat
CREATE TABLE IF NOT EXISTS autopilot (
    pattern  TEXT PRIMARY KEY,
    status   TEXT NOT NULL,            -- aktiv | aus
    changed  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS autopilot_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern    TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    ts         REAL NOT NULL,
    expected   TEXT NOT NULL,
    prev       TEXT,                   -- JSON: Zustand davor, für Rückgängig
    result     TEXT                    -- NULL | rückgängig
);
CREATE INDEX IF NOT EXISTS idx_autopilot_runs ON autopilot_runs(pattern, ts);

-- Einstellungen aus der Oberfläche
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL              -- JSON
);
"""

# Spalten, die nach v0.1 dazukamen – bestehende Datenbanken werden ergänzt.
MIGRATIONS = {
    "kind": "TEXT NOT NULL DEFAULT 'action'",
    "area_id": "TEXT",
    "automation": "TEXT",
    "context": "TEXT",
}
SUGGESTION_MIGRATIONS = {"since": "REAL", "paused": "REAL"}

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
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(suggestions)")}
        for col, decl in SUGGESTION_MIGRATIONS.items():
            if col not in cols:
                self.db.execute(f"ALTER TABLE suggestions ADD COLUMN {col} {decl}")
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
            " WHERE result IS NOT NULL AND result != 'autopilot' AND opened >= ?"
            " GROUP BY pattern"
            " ORDER BY pattern", (cutoff,)).fetchall()

    def last_predictions(self, pattern: str, n: int, since: float = 0) -> list[str]:
        """Ergebnisse der jüngsten Vorhersagen (ohne Autopilot-Läufe), neueste zuerst."""
        return [r[0] for r in self.db.execute(
            "SELECT result FROM predictions WHERE pattern = ? AND result IS NOT NULL"
            " AND result != 'autopilot' AND opened >= ? ORDER BY id DESC LIMIT ?",
            (pattern, since, n))]

    def autopilot(self) -> dict[str, tuple[str, float]]:
        return {p: (s, c) for p, s, c in self.db.execute(
            "SELECT pattern, status, changed FROM autopilot")}

    def set_autopilot(self, pattern: str, status: str, now: float):
        self.db.execute(
            "INSERT INTO autopilot (pattern, status, changed) VALUES (?,?,?)"
            " ON CONFLICT(pattern) DO UPDATE SET status = excluded.status,"
            " changed = excluded.changed", (pattern, status, now))
        self.db.commit()

    def add_autopilot_run(self, *, pattern, entity_id, ts, expected, prev) -> int:
        cur = self.db.execute(
            "INSERT INTO autopilot_runs (pattern, entity_id, ts, expected, prev)"
            " VALUES (?,?,?,?,?)", (pattern, entity_id, ts, expected, json.dumps(prev)))
        self.db.commit()
        return cur.lastrowid

    def autopilot_run(self, rid) -> dict | None:
        row = self.db.execute(
            "SELECT id, pattern, entity_id, ts, expected, prev, result"
            " FROM autopilot_runs WHERE id = ?", (rid,)).fetchone()
        if not row:
            return None
        keys = ("id", "pattern", "entity_id", "ts", "expected", "prev", "result")
        run = dict(zip(keys, row))
        run["prev"] = json.loads(run["prev"]) if run["prev"] else None
        return run

    def recent_autopilot_run(self, entity_id: str, since: float) -> dict | None:
        row = self.db.execute(
            "SELECT id FROM autopilot_runs WHERE entity_id = ? AND ts >= ?"
            " AND result IS NULL ORDER BY id DESC LIMIT 1", (entity_id, since)).fetchone()
        return self.autopilot_run(row[0]) if row else None

    def undo_autopilot_run(self, rid):
        self.db.execute("UPDATE autopilot_runs SET result = 'rückgängig' WHERE id = ?", (rid,))
        self.db.commit()

    def autopilot_results(self, pattern: str, n: int) -> list[str | None]:
        return [r[0] for r in self.db.execute(
            "SELECT result FROM autopilot_runs WHERE pattern = ? ORDER BY id DESC LIMIT ?",
            (pattern, n))]

    def autopilot_counts(self) -> dict[str, tuple[int, int]]:
        """pattern → (Läufe, davon rückgängig)."""
        return {p: (n, u) for p, n, u in self.db.execute(
            "SELECT pattern, COUNT(*), SUM(result = 'rückgängig') FROM autopilot_runs"
            " GROUP BY pattern")}

    def setting(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value):
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))
        self.db.commit()

    def add_suggestion(self, *, pattern, text, config, offered) -> int:
        cur = self.db.execute(
            "INSERT INTO suggestions (pattern, text, config, offered) VALUES (?,?,?,?)",
            (pattern, text, json.dumps(config), offered))
        self.db.commit()
        return cur.lastrowid

    def answer_suggestion(self, sid, *, status, answered, automation=None):
        self.db.execute(
            "UPDATE suggestions SET status = ?, answered = ?, automation = ?, since = ?"
            " WHERE id = ?", (status, answered, automation, answered, sid))
        self.db.commit()

    def suggestion(self, sid) -> dict | None:
        row = self.db.execute(
            "SELECT pattern, text, config, status FROM suggestions WHERE id = ?",
            (sid,)).fetchone()
        if not row:
            return None
        return {"pattern": row[0], "text": row[1], "config": json.loads(row[2]),
                "status": row[3]}

    def automated(self) -> list[dict]:
        """Angelegte Automationen, die noch gemessen oder aufgeräumt werden."""
        rows = self.db.execute(
            "SELECT id, automation, status, since, paused FROM suggestions"
            " WHERE status IN ('angenommen', 'pausiert') AND automation IS NOT NULL"
            " ORDER BY id").fetchall()
        keys = ("id", "automation", "status", "since", "paused")
        return [dict(zip(keys, row)) for row in rows]

    def set_automation_status(self, sid, status, *, since=None, paused=None):
        self.db.execute(
            "UPDATE suggestions SET status = ?, since = COALESCE(?, since), paused = ?"
            " WHERE id = ?", (status, since, paused, sid))
        self.db.commit()

    def last_suggestion(self, pattern) -> tuple[str, float] | None:
        """Status und Zeitpunkt (Antwort, sonst Angebot) des jüngsten Vorschlags."""
        return self.db.execute(
            "SELECT status, COALESCE(answered, offered) FROM suggestions"
            " WHERE pattern = ? ORDER BY id DESC LIMIT 1", (pattern,)).fetchone()

    def suggestions(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, pattern, text, offered, status, answered, automation, paused"
            " FROM suggestions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        keys = ("id", "pattern", "text", "offered", "status", "answered", "automation",
                "paused")
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
