"""Folge- und Kontextmuster im Schattenmodus, dann Autopilot mit Rückgängig.

Aufruf:  python3 habit_learner/tests/test_autopilot.py
"""
import asyncio
import datetime as dt
import os
import time
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from autopilot import MIN_RECORD, Autopilot  # noqa: E402
from shadow import pattern_id  # noqa: E402
from test_analysis import DAYS, ZONE  # noqa: E402
from test_shadow import key, results  # noqa: E402
import sqlite3  # noqa: E402
from analysis import analyse  # noqa: E402
from shadow import Shadow  # noqa: E402
from store import Store  # noqa: E402
from test_analysis import build  # noqa: E402

TMP = os.environ.get("TMPDIR", "/tmp")
WECKER = "input_datetime.wecker → geändert"
OFF = key("light.schlafzimmer", "off")
ON = key("light.schlafzimmer", "on")


class FakeLearner:
    def __init__(self, store, shadow):
        self.store, self.shadow = store, shadow
        self.states = {"person.alex": {"state": "home"},
                       "light.schlafzimmer": {"state": "on", "attributes": {}},
                       "light.tisch": {"state": "off", "attributes": {}}}
        self.suggester = type("S", (), {"name": lambda _s, e: e,
                                        "phones_for": lambda _s, u: ["mobile_app_pixel"]})()
        self.calls = []

    def current_key(self, entity_id):
        s = self.states.get(entity_id)
        return s and key(entity_id, s["state"], s.get("attributes"))

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, data))
        if domain == "light":
            self.states[data["entity_id"]]["state"] = "off" if service == "turn_off" else "on"


async def settle():
    for _ in range(3):
        await asyncio.sleep(0)


def evening(l, day, minute=22 * 60):
    """Ein Abend: Wecker stellen, Ereignisse bis 30 min später durchticken."""
    t0 = 86400 * day + minute * 60.0 + 1.8e9
    l.states["light.schlafzimmer"] = {"state": "on", "attributes": {}}
    l.shadow.on_trigger(t0, WECKER, l.states, l.current_key)
    return t0


def fresh(path):
    if os.path.exists(path):
        os.remove(path)
    now = build(path)
    store = Store(path)
    shadow = Shadow(store, ZONE)
    shadow.set_patterns([p for p in analyse(sqlite3.connect(path), now=now, zone=ZONE)
                         if p.entity_id in ("light.schlafzimmer", "light.tisch")])
    store.db.execute("DELETE FROM predictions")
    return store, shadow


async def scenario(path):
    store, shadow = fresh(path)
    l = FakeLearner(store, shadow)

    # 1) Folgemuster: Wecker → 5 min später Licht aus = Treffer
    t0 = evening(l, DAYS)
    l.states["light.schlafzimmer"] = {"state": "off", "attributes": {}}
    shadow.on_action(t0 + 300, "light.schlafzimmer", OFF)
    assert results(store) == {"light.schlafzimmer": "treffer"}, results(store)

    # 2) Kontext: Tischlampe abends eingeschaltet, auf Rot gestellt = Treffer
    store.db.execute("DELETE FROM predictions")
    tisch = next(p for p in shadow.patterns if p.anchor == "kontext")
    red = tisch.settings["abends"]
    ts = dt.datetime(2026, 11, 2, 19, 30, tzinfo=ZONE).timestamp()
    shadow.on_action(ts, "light.tisch", key("light.tisch", "on", {"brightness": 255}),
                     old_key=key("light.tisch", "off"), states=l.states,
                     current_key=l.current_key)
    shadow.on_action(ts + 20, "light.tisch", red["key"])
    assert results(store) == {"light.tisch": "treffer"}, results(store)

    # 3) Autopilot: erst nach 10 von 10 Treffern frei
    ap = Autopilot(l, {"autopilot": True})
    shadow.on_autopilot = ap.on_act
    wecker = next(p for p in shadow.patterns if p.anchor == "folge")
    pid = pattern_id(wecker)
    store.db.execute("DELETE FROM predictions")
    for i in range(MIN_RECORD - 1):
        store.add_prediction(pattern=pid, entity_id=wecker.entity_id, expected=OFF,
                             opened=i, closes=i + 1)
    store.db.execute("UPDATE predictions SET result = 'treffer'")
    ap.refresh(shadow.patterns)
    assert pid not in shadow.autopilot
    store.add_prediction(pattern=pid, entity_id=wecker.entity_id, expected=OFF,
                         opened=99, closes=100)
    store.db.execute("UPDATE predictions SET result = 'treffer'")
    ap.refresh(shadow.patterns)
    assert pid in shadow.autopilot
    await settle()
    assert [c[2]["title"] for c in l.calls if c[0] == "notify"] == ["Autopilot übernimmt"]

    # 4) Wecker gestellt → nach 5 min schaltet der Autopilot selbst, mit Rückgängig
    l.calls.clear()
    t0 = evening(l, DAYS + 1)
    shadow.tick(t0 + 120, l.states, l.current_key)
    assert not l.calls, "nicht vor der erwarteten Zeit"
    shadow.tick(t0 + 301, l.states, l.current_key)
    await settle()
    assert ("light", "turn_off", {"entity_id": "light.schlafzimmer"}) in l.calls, l.calls
    note = next(c for c in l.calls if c[0] == "notify")
    undo = note[2]["data"]["actions"][0]["action"]
    assert undo.startswith("HABIT_UNDO_"), note

    # 5) Rückgängig → Licht wieder an; zweites Mal (von Hand) → Autopilot aus
    ap.on_notification_action({"data": {"action": undo}})
    await settle()
    assert ("light", "turn_on", {"entity_id": "light.schlafzimmer"}) in l.calls, l.calls
    assert pid in shadow.autopilot, "einmal rückgängig reicht nicht"
    t0 = evening(l, DAYS + 2)
    shadow.tick(t0 + 301, l.states, l.current_key)
    await settle()
    ap.on_manual(time.time(), "light.schlafzimmer", ON)   # Läufe tragen echte Uhrzeit
    await settle()
    assert pid not in shadow.autopilot
    assert store.autopilot()[pid][0] == "aus"

    # 6) Autopilot-Läufe zählen nicht in die Schattenbilanz
    assert all(total == hits for _, hits, total in store.prediction_stats(10 ** 6)
               if _ == pid), store.prediction_stats(10 ** 6)

    # 7) Ohne Option nie
    off = Autopilot(l, {})
    off.refresh(shadow.patterns)
    assert shadow.autopilot == set()
    print("alle Prüfungen bestanden")


if __name__ == "__main__":
    asyncio.run(scenario(os.path.join(TMP, "habits_autopilot.db")))
