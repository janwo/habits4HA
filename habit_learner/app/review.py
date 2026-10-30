"""Angelegte Automationen nachmessen – und wieder aufräumen.

Nach jedem Lauf einer vom Habit Learner angelegten Automation wird geschaut,
was danach passiert ist:

- rückgängig  – jemand hat das Gerät innerhalb von UNDO_MIN wieder umgeschaltet
- korrigiert  – gleicher Zustand, aber z. B. anders gedimmt
- gelassen    – nichts geändert, und jemand war zuhause, hätte es also bemerkt

Wird eine Automation von den letzten LOOK_BACK bewerteten Läufen mindestens
UNDO_LIMIT-mal rückgängig gemacht, wird sie pausiert (in HA deaktiviert) und
per Handy gemeldet. Schaltet sie jemand wieder ein, gilt sie erneut als
angenommen und wird ab dann frisch gemessen. Bleibt sie DELETE_DAYS pausiert,
wird sie gelöscht. Angefasst werden nur Automationen `habit_learner_*`.
"""
import json
import logging

log = logging.getLogger("habit_learner")

UNDO_MIN = 15
LOOK_BACK = 5
UNDO_LIMIT = 3
DELETE_DAYS = 14


def judge_runs(db, automation_entity: str, since: float) -> list[dict]:
    """Läufe einer Automation seit `since` mit Bewertung (älteste zuerst)."""
    runs = db.execute(
        "SELECT ts, entity_id, new_state, attributes, context FROM events"
        " WHERE automation = ? AND kind = 'action' AND ts >= ? ORDER BY ts",
        (automation_entity, since)).fetchall()
    result = []
    for ts, entity_id, state, attrs, context in runs:
        after = db.execute(
            "SELECT new_state, attributes FROM events WHERE entity_id = ?"
            " AND kind = 'action' AND origin IN ('manual', 'device')"
            " AND ts > ? AND ts <= ? ORDER BY ts LIMIT 1",
            (entity_id, ts, ts + UNDO_MIN * 60)).fetchone()
        if after and after[0] != state:
            verdict = "rückgängig"
        elif after and after[1] != attrs:
            verdict = "korrigiert"
        elif (json.loads(context) if context else {}).get("home"):
            verdict = "gelassen"
        else:
            continue                  # niemand da – Schweigen sagt nichts (D06)
        result.append({"ts": ts, "entity_id": entity_id, "verdict": verdict})
    return result


def should_pause(runs: list[dict]) -> bool:
    recent = runs[-LOOK_BACK:]
    return sum(r["verdict"] == "rückgängig" for r in recent) >= UNDO_LIMIT


def balance(runs: list[dict]) -> dict:
    out = {"gelassen": 0, "korrigiert": 0, "rückgängig": 0}
    for r in runs:
        out[r["verdict"]] += 1
    return out


class Reviewer:
    def __init__(self, learner):
        self.l = learner

    def entity_for(self, automation_id: str) -> str | None:
        """automation.* in HA – die Entitäts-ID hängt am Alias, die Config-ID nicht."""
        return next((e for e, s in self.l.states.items()
                     if e.startswith("automation.")
                     and s.get("attributes", {}).get("id") == automation_id), None)

    def runs(self, s: dict) -> list[dict]:
        entity = self.entity_for(s["automation"])
        return judge_runs(self.l.store.db, entity, s["since"]) if entity else []

    async def check(self, now: float):
        store = self.l.store
        for s in store.automated():
            entity = self.entity_for(s["automation"])
            if not entity:
                continue                  # (noch) nicht geladen oder von Hand gelöscht
            state = self.l.states[entity].get("state")
            if s["status"] == "angenommen":
                runs = judge_runs(store.db, entity, s["since"])
                if should_pause(runs):
                    await self.pause(s, entity, runs, now)
            elif s["status"] == "pausiert":
                if state == "on":         # jemand hat sie zurückgeholt
                    store.set_automation_status(s["id"], "angenommen", since=now)
                    log.info("Automation %s wieder eingeschaltet – wird neu gemessen",
                             s["automation"])
                elif now - s["paused"] >= DELETE_DAYS * 86400:
                    await self.l.rest("DELETE", f"/config/automation/config/{s['automation']}")
                    store.set_automation_status(s["id"], "gelöscht")
                    log.info("Automation %s nach %d Tagen Pause gelöscht",
                             s["automation"], DELETE_DAYS)

    async def pause(self, s: dict, entity: str, runs: list[dict], now: float):
        await self.l.call_service("automation", "turn_off", {"entity_id": entity})
        self.l.store.set_automation_status(s["id"], "pausiert", paused=now)
        recent = runs[-LOOK_BACK:]
        undone = sum(r["verdict"] == "rückgängig" for r in recent)
        name = self.l.suggester.name(entity)
        text = (f"„{name}“ wurde {undone} von {len(recent)} Mal rückgängig gemacht und "
                f"ist jetzt pausiert. Ohne Einschalten wird sie in {DELETE_DAYS} Tagen gelöscht.")
        log.info("Automation %s pausiert: %s", s["automation"], text)
        for phone in self.l.suggester.phones_for(None):
            await self.l.call_service("notify", phone, {
                "title": "Automation pausiert", "message": text,
                "data": {"tag": f"habit_{s['id']}"}})
