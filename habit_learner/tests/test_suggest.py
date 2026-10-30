"""Vorschlag im Moment mit einem nachgebauten Home Assistant prüfen.

Aufruf:  python3 habit_learner/tests/test_suggest.py
"""
import asyncio
import datetime as dt
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from analysis import analyse  # noqa: E402
from shadow import pattern_id  # noqa: E402
from store import Store  # noqa: E402
from suggest import Suggester, build_automation, in_quiet_hours  # noqa: E402
from test_analysis import DAYS, START, ZONE, build  # noqa: E402

TMP = os.environ.get("TMPDIR", "/tmp")


class FakeHA:
    """Das, was der Suggester vom Learner braucht."""

    def __init__(self, store):
        self.store, self.zone = store, ZONE
        self.states = {
            "person.alex": {"state": "home", "attributes": {
                "user_id": "u-alex", "device_trackers": ["device_tracker.pixel"]}},
            "person.gast": {"state": "home", "attributes": {
                "user_id": "u-gast", "device_trackers": ["device_tracker.iphone"]}},
            "light.wohnzimmer": {"state": "on", "attributes": {"friendly_name": "Wohnzimmer"}},
            "switch.kaffee": {"state": "on", "attributes": {"friendly_name": "Kaffeemaschine"}},
            "media_player.wohnzimmer_lautsprecher": {"state": "idle", "attributes": {}},
            "media_player.kueche": {"state": "idle", "attributes": {}},
            "tts.google": {"state": "unknown", "attributes": {}},
        }
        self.platforms = {"media_player.wohnzimmer_lautsprecher": "cast",
                          "media_player.kueche": "cast", "media_player.tv": "androidtv"}
        self.areas = {"light.wohnzimmer": "wohnzimmer",
                      "media_player.wohnzimmer_lautsprecher": "wohnzimmer",
                      "media_player.kueche": "kueche"}
        self.calls, self.posts = [], []

    async def request(self, type_, **_):
        assert type_ == "get_services"
        return {"notify": {"mobile_app_pixel": {}, "mobile_app_iphone": {},
                           "persistent_notification": {}}}

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, data))

    async def rest(self, method, path, payload):
        self.posts.append((method, path, payload))


def ts(day, minute):
    d = dt.datetime.combine(START + dt.timedelta(days=day), dt.time(), ZONE)
    return (d + dt.timedelta(minutes=minute)).timestamp()


async def scenario(path):
    if os.path.exists(path):
        os.remove(path)
    now = build(path)
    patterns = {p.entity_id: p for p in analyse(sqlite3.connect(path), now=now, zone=ZONE)}
    store = Store(path)
    ha = FakeHA(store)
    s = Suggester(ha, {"announce": True})
    await s.discover()
    s.set_patterns(list(patterns.values()))
    wohn, kaffee = patterns["light.wohnzimmer"], patterns["switch.kaffee"]

    # Automationen
    a = build_automation(wohn, "Wohnzimmer")
    assert a["triggers"][0] == {"trigger": "numeric_state", "entity_id": "sun.sun",
                                "attribute": "elevation", "below": round(wohn.center, 1)}, a
    assert a["actions"][0] == {"action": "light.turn_on",
                               "target": {"entity_id": "light.wohnzimmer"},
                               "data": {"brightness": 128, "color_temp_kelvin": 2700}}, a
    k = build_automation(kaffee, "Kaffeemaschine")
    assert k["triggers"][0]["trigger"] == "time" and k["triggers"][0]["at"][:4] == "06:5", k
    assert {"condition": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]} \
        in k["conditions"], k

    # Folgemuster: Auslöser + Verzögerung
    f = build_automation(patterns["light.schlafzimmer"], "Schlafzimmer")
    assert f["triggers"] == [{"trigger": "state", "entity_id": "input_datetime.wecker"}], f
    assert f["actions"][0] == {"delay": {"minutes": 5}}, f
    assert f["actions"][1]["action"] == "light.turn_off", f

    # Kontext: beim Einschalten je nach Tageszeit einstellen
    tisch = next(p for p in analyse(sqlite3.connect(path), now=now, zone=ZONE)
                 if p.anchor == "kontext")
    c = build_automation(tisch, "Tisch")
    assert c["triggers"][0] == {"trigger": "state", "entity_id": "light.tisch",
                                "from": "off", "to": "on"}, c
    choose = c["actions"][0]["choose"]
    assert choose[0]["conditions"][0]["after"] == "05:00:00", choose
    assert choose[1]["sequence"][0]["data"]["hs_color"] == [0, 100], choose

    # 1) Treffer Wohnzimmer abends von Alex → nur Alex’ Handy, Ansage im Wohnzimmer
    s.on_hit(pattern_id(wohn), "u-alex", ts(DAYS, 19 * 60))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    notes = [c for c in ha.calls if c[0] == "notify"]
    assert [c[1] for c in notes] == ["mobile_app_pixel"], ha.calls
    assert "Wohnzimmer" in notes[0][2]["message"], notes
    tts = [c for c in ha.calls if c[0] == "tts"]
    assert tts and tts[0][2]["media_player_entity_id"] == "media_player.wohnzimmer_lautsprecher"

    # 2) Am selben Tag kein zweiter Vorschlag
    ha.calls.clear()
    s.on_hit(pattern_id(kaffee), "u-alex", ts(DAYS, 19 * 60 + 5))
    await asyncio.sleep(0)
    assert ha.calls == [], ha.calls

    # 3) Übernehmen → Automation wird angelegt
    sid = store.db.execute("SELECT MAX(id) FROM suggestions").fetchone()[0]
    s.on_notification_action({"data": {"action": f"HABIT_ACCEPT_{sid}"}})
    for _ in range(3):
        await asyncio.sleep(0)
    assert ha.posts and ha.posts[0][1] == f"/config/automation/config/habit_learner_{sid}"
    assert store.suggestion(sid)["status"] == "angenommen"

    # 4) Bereits beantwortet → nicht erneut vorschlagen, auch an einem neuen Tag
    ha.calls.clear()
    s.on_hit(pattern_id(wohn), "u-alex", ts(DAYS + 1, 19 * 60))
    await asyncio.sleep(0)
    assert ha.calls == [], ha.calls

    # 5) Kaffee nachts, ohne bekannten Nutzer → Handys aller Anwesenden, keine Ansage
    ha.calls.clear()
    s.on_hit(pattern_id(kaffee), None, ts(DAYS + 1, 6 * 60 + 50))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert sorted(c[1] for c in ha.calls if c[0] == "notify") == \
        ["mobile_app_iphone", "mobile_app_pixel"], ha.calls
    assert not [c for c in ha.calls if c[0] == "tts"], "keine Ansage in der Ruhezeit"

    # 6) Später → nach 3 Tagen wieder erlaubt
    sid = store.db.execute("SELECT MAX(id) FROM suggestions").fetchone()[0]
    s.on_notification_action({"data": {"action": f"HABIT_LATER_{sid}"}})
    await asyncio.sleep(0)
    later = store.db.execute("SELECT answered FROM suggestions WHERE id=?", (sid,)).fetchone()[0]
    assert not s.may_offer(pattern_id(kaffee), later + 86400)
    assert s.may_offer(pattern_id(kaffee), later + 4 * 86400)

    # Anwesenheit als eigene Entität
    from analysis import Presence, anyone_home, find_presence, is_home
    from suggest import presence_condition
    states = {"binary_sensor.presence": {"state": "off"}, "person.alex": {"state": "home"},
              "binary_sensor.kueche_bewegung": {"state": "on"}}
    assert find_presence(states) == ("binary_sensor.presence",)
    assert anyone_home(states, ())                                   # ohne: Personen
    assert not anyone_home(states, ("binary_sensor.presence",))      # mit: nur sie zählt
    assert is_home("zone.home", "2") and not is_home("zone.home", "0")
    assert presence_condition({"binary_sensor.presence": "off"}) == \
        {"condition": "state", "entity_id": "binary_sensor.presence", "state": "on"}
    assert presence_condition({"zone.home": "1"})["condition"] == "numeric_state"
    assert presence_condition(None)["entity_id"] == "zone.home"
    a2 = build_automation(wohn, "Wohnzimmer", {"binary_sensor.presence": "on"})
    assert a2["conditions"][0]["entity_id"] == "binary_sensor.presence", a2
    store.add(ts=1, entity_id="binary_sensor.presence", old_state=None, new_state="on",
              attributes=None, origin="device", user_id=None, context_id=None, kind="trigger")
    store.add(ts=100, entity_id="binary_sensor.presence", old_state="on", new_state="off",
              attributes=None, origin="device", user_id=None, context_id=None, kind="trigger")
    pr = Presence(store.db, ("binary_sensor.presence",))
    assert pr.home_at(50) is True and pr.home_at(150) is False

    assert in_quiet_hours(23 * 60, "22:00", "07:00") and in_quiet_hours(6 * 60, "22:00", "07:00")
    assert not in_quiet_hours(12 * 60, "22:00", "07:00")
    print("alle Prüfungen bestanden")


if __name__ == "__main__":
    asyncio.run(scenario(os.path.join(TMP, "habits_suggest.db")))
