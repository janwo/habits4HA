"""Schattenmodus an simulierten Tagen prüfen – nutzt den Testdatensatz
aus test_analysis.py für die Muster.

Aufruf:  python3 habit_learner/tests/test_shadow.py
"""
import datetime as dt
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from analysis import analyse, outcome  # noqa: E402
from shadow import Shadow  # noqa: E402
from store import Store  # noqa: E402
from test_analysis import DAYS, START, ZONE, build, sun  # noqa: E402

TMP = os.environ.get("TMPDIR", "/tmp")


def key(entity_id, state, attrs=None):
    return f"{entity_id} → {outcome(entity_id, state, attrs or {})}"


class Day:
    """Ein simulierter Tag mit Zuständen und Uhr."""

    def __init__(self, shadow: Shadow, day: int, home=True):
        self.shadow, self.day = shadow, day
        self.states = {"person.alex": {"state": "home" if home else "not_home"},
                       "switch.kaffee": {"state": "off", "attributes": {}},
                       "light.wohnzimmer": {"state": "off", "attributes": {}}}
        self.elev = None

    def ts(self, minute):
        d = dt.datetime.combine(START + dt.timedelta(days=self.day), dt.time(), ZONE)
        return (d + dt.timedelta(minutes=minute)).timestamp()

    def current_key(self, entity_id):
        s = self.states.get(entity_id)
        return s and key(entity_id, s["state"], s.get("attributes"))

    def run(self, until, actions=()):
        """Minütlich ticken und die Sonne mitlaufen lassen; Aktionen zur Minute ausführen."""
        todo = sorted(actions)
        for minute in range(0, until):
            now = self.ts(minute)
            elev, rising = sun(self.day, minute)
            self.shadow.on_sun(now, self.elev, elev, rising, self.states, self.current_key)
            self.elev = elev
            while todo and todo[0][0] <= minute:
                _, entity_id, state, attrs = todo.pop(0)
                self.states[entity_id] = {"state": state, "attributes": attrs}
                self.shadow.on_action(now, entity_id, key(entity_id, state, attrs))
            self.shadow.tick(now, self.states, self.current_key)


def results(store):
    return {e: r for e, r in store.db.execute(
        "SELECT entity_id, result FROM predictions ORDER BY id")}


def fresh(path):
    if os.path.exists(path):
        os.remove(path)
    now = build(path)
    store = Store(path)
    shadow = Shadow(store, ZONE)
    shadow.set_patterns([p for p in analyse(sqlite3.connect(path), now=now, zone=ZONE)
                         if p.entity_id in ("switch.kaffee", "light.wohnzimmer")])
    store.db.execute("DELETE FROM predictions")
    return store, shadow


def main():
    path = os.path.join(TMP, "habits_shadow.db")
    warm = {"brightness": 128, "color_temp_kelvin": 2700}
    day = DAYS          # Montag nach dem Testzeitraum
    sunset = 19 * 60 - 2 * day

    # 1) Kaffee pünktlich, Wohnzimmer erst zu hell, dann richtig gedimmt → zwei Treffer
    store, shadow = fresh(path)
    Day(shadow, day).run(24 * 60, [
        (6 * 60 + 53, "switch.kaffee", "on", {}),
        (sunset + 12, "light.wohnzimmer", "on", {"brightness": 255, "color_temp_kelvin": 2700}),
        (sunset + 13, "light.wohnzimmer", "on", warm),
    ])
    assert results(store) == {"switch.kaffee": "treffer", "light.wohnzimmer": "treffer"}, \
        results(store)

    # 2) Nichts getan → verpasst; Wohnzimmer nur hell → abweichend
    store, shadow = fresh(path)
    Day(shadow, day).run(24 * 60, [
        (sunset + 12, "light.wohnzimmer", "on", {"brightness": 255, "color_temp_kelvin": 2700}),
    ])
    assert results(store) == {"switch.kaffee": "verpasst", "light.wohnzimmer": "abweichend"}, \
        results(store)

    # 3) Niemand zuhause → keine Vorhersage
    store, shadow = fresh(path)
    Day(shadow, day, home=False).run(24 * 60)
    assert results(store) == {}, results(store)

    # 4) Kaffee schon an → keine Vorhersage dafür
    store, shadow = fresh(path)
    d = Day(shadow, day)
    d.states["switch.kaffee"] = {"state": "on", "attributes": {}}
    d.run(12 * 60)
    assert results(store) == {}, results(store)

    # 5) Samstag: Kaffee-Muster gilt nur werktags
    store, shadow = fresh(path)
    Day(shadow, day + 5).run(12 * 60)
    assert "switch.kaffee" not in results(store), results(store)

    print("\n".join(shadow.report()) or "(keine Bilanz)")
    print("\nalle Prüfungen bestanden")


if __name__ == "__main__":
    main()
