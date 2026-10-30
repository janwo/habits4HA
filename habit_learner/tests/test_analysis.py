"""Testdatensatz mit erfundenen Wochen – prüft, dass die Analyse die
eingebauten Muster findet und keine falschen.

Aufruf:  python3 habit_learner/tests/test_analysis.py
"""
import datetime as dt
import os
import random
import sqlite3
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from analysis import analyse, confidence, report  # noqa: E402
from store import Store  # noqa: E402

ZONE = ZoneInfo("Europe/Berlin")
START = dt.date(2026, 9, 14)            # Montag
DAYS = 42
VACATION = range(20, 24)                # Tage, an denen niemand zuhause ist


def sun(day: int, minute: float) -> tuple[float, bool]:
    """Vereinfachte Sonne: Aufgang wird täglich 2 min später, Untergang 2 min früher."""
    sunrise = 7 * 60 + 2 * day
    sunset = 19 * 60 - 2 * day
    if minute < 12 * 60:
        return (minute - sunrise) / 4.5, True
    return (sunset - minute) / 4.5, False


def build(path: str) -> float:
    rnd = random.Random(42)
    store = Store(path)

    def at(day: int, minute: float) -> float:
        d = dt.datetime.combine(START + dt.timedelta(days=day), dt.time(), ZONE)
        return (d + dt.timedelta(minutes=minute)).timestamp()

    def action(day, minute, entity_id, state, attrs=None, origin="manual", automation=None,
               old=None):
        elev, rising = sun(day, minute)
        home = [] if day in VACATION else ["person.alex"]
        store.add(ts=at(day, minute), entity_id=entity_id, old_state=old, new_state=state,
                  attributes=attrs, origin=origin, user_id="u", context_id=None,
                  automation=automation,
                  context={"sun_elevation": elev, "sun_rising": rising, "home": home})

    def person(day, minute, state):
        store.add(ts=at(day, minute), entity_id="person.alex", old_state=None,
                  new_state=state, attributes=None, origin="device", user_id=None,
                  context_id=None, kind="trigger")

    def alarm(day, minute):
        store.add(ts=at(day, minute), entity_id="input_datetime.wecker", old_state="07:00:00",
                  new_state="06:30:00", attributes=None, origin="manual", user_id="u",
                  context_id=None, kind="trigger")

    person(0, 0, "home")
    person(VACATION.start, 8 * 60, "not_home")
    person(VACATION.stop - 1, 20 * 60, "home")

    for day in range(DAYS):
        weekday = (START + dt.timedelta(days=day)).weekday()
        away = day in VACATION

        # 1) Kaffee werktags ~6:50, gelegentlich vergessen
        if weekday < 5 and not away and rnd.random() < 0.9:
            action(day, 6 * 60 + 50 + rnd.gauss(0, 4), "switch.kaffee", "on")

        # 2) Wohnzimmer bei Sonnenhöhe ~−3° an, 50 % warm – Uhrzeit wandert mit
        if not away and rnd.random() < 0.85:
            sunset = 19 * 60 - 2 * day
            action(day, sunset + 3 * 4.5 + rnd.gauss(0, 2), "light.wohnzimmer", "on",
                   {"brightness": 128, "color_temp_kelvin": 2700})

        # 3) Schlafzimmer aus 5 min nach Wecker stellen – Wecker zu wechselnden Zeiten
        if not away:
            minute = rnd.uniform(22 * 60, 23 * 60 + 50)
            alarm(day, minute)
            action(day, minute + 5 + rnd.gauss(0, 0.5), "light.schlafzimmer", "off")

        # 4) Flur: Automation um 18:00, manchmal auch von Hand
        if not away:
            action(day, 18 * 60, "light.flur", "on", origin="automation",
                   automation="automation.flur")
            if rnd.random() < 0.3:
                action(day, 18 * 60 + rnd.gauss(0, 3), "light.flur", "on")

        # 6) Tischlampe zu wechselnden Zeiten an: morgens warmweiß, abends rot
        if not away:
            m = rnd.uniform(6 * 60, 9 * 60)
            action(day, m, "light.tisch", "on", {"brightness": 128, "color_temp_kelvin": 2700},
                   old="off")
            action(day, m + 60, "light.tisch", "off", old="on")
            m = rnd.uniform(18 * 60, 21 * 60 + 30)
            action(day, m, "light.tisch", "on", {"brightness": 64, "hs_color": [0, 100],
                                                  "color_mode": "hs"}, old="off")
            action(day, m + 30, "light.tisch", "off", old="on")

        # 5) Rauschen: Küche zu zufälligen Zeiten
        if not away and rnd.random() < 0.5:
            action(day, rnd.uniform(7 * 60, 23 * 60), "light.kueche", "on")

        # 7) Bad zweimal am Tag: morgens ~6:45, abends ~21:30
        if not away:
            action(day, 6 * 60 + 45 + rnd.gauss(0, 4), "light.bad", "on")
            action(day, 21 * 60 + 30 + rnd.gauss(0, 4), "light.bad", "on")

        # 8) Radio erst ~7:00, ab Tag 26 (neuer Job) ~8:15
        if not away:
            base = 7 * 60 if day < 26 else 8 * 60 + 15
            action(day, base + rnd.gauss(0, 3), "switch.radio", "on")

    return at(DAYS, 0)


def main():
    path = "/tmp/habits_test.db" if len(sys.argv) < 2 else sys.argv[1]
    if os.path.exists(path):
        os.remove(path)
    now = build(path)
    patterns = analyse(sqlite3.connect(path), now=now, zone=ZONE)
    for line in report(patterns):
        print(line)

    found = {p.entity_id: p for p in patterns}
    ready = {p.entity_id for p in patterns if p.ready}

    kaffee = found["switch.kaffee"]
    assert kaffee.ready and kaffee.anchor == "zeit" and kaffee.days == "werktags", kaffee
    assert abs(kaffee.center - (6 * 60 + 50)) < 5, kaffee.center

    wohn = found["light.wohnzimmer"]
    assert wohn.ready and wohn.anchor == "sonne" and wohn.rising is False, wohn
    assert wohn.days == "täglich", wohn.days
    assert abs(wohn.center + 3) < 1, wohn.center
    assert "50 % warm" in wohn.key, wohn.key

    schlaf = found["light.schlafzimmer"]
    assert schlaf.ready and schlaf.anchor == "folge", schlaf
    assert schlaf.trigger == "input_datetime.wecker → geändert" and abs(schlaf.center - 5) < 1

    tisch = [p for p in patterns if p.entity_id == "light.tisch" and p.anchor == "kontext"]
    assert tisch and tisch[0].ready, tisch
    assert set(tisch[0].settings) == {"morgens", "abends"}, tisch[0].settings
    assert "warm" in tisch[0].settings["morgens"]["key"]
    assert "farbig" in tisch[0].settings["abends"]["key"]
    assert not [p for p in patterns if p.entity_id == "light.tisch" and p.ready
                and p.anchor != "kontext"], "Tischlampe hat keine feste Zeit"
    assert "light.kueche" not in ready, "Rauschen darf kein Muster sein"
    assert found["light.flur"].automated and "light.flur" not in ready, found["light.flur"]
    # Bayes: 5 von 5 reicht, 3 von 3 noch nicht; 30 von 50 ist zu unsicher
    assert confidence(5, 5) >= 0.9 > confidence(3, 3), (confidence(5, 5), confidence(3, 3))
    assert confidence(30, 50) < 0.9 < confidence(40, 50)

    bad = sorted((p for p in patterns if p.entity_id == "light.bad" and p.ready),
                 key=lambda p: p.center)
    assert [round(p.center / 60) for p in bad] == [7, 22], [p.describe() for p in bad]
    from shadow import pattern_id
    assert len({pattern_id(p) for p in bad}) == 2, "zwei Zeitpunkte, zwei Muster"

    radio = found["switch.radio"]
    assert radio.ready and radio.changed_at, radio
    assert abs(radio.center - (8 * 60 + 15)) < 5, radio.center
    assert radio.chances <= DAYS - 26, radio.chances
    print("\nalle Prüfungen bestanden")


if __name__ == "__main__":
    main()
