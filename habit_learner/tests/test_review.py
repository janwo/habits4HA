"""Angelegte Automationen nachmessen, pausieren und löschen.

Aufruf:  python3 habit_learner/tests/test_review.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from review import DELETE_DAYS, Reviewer, judge_runs  # noqa: E402
from store import Store  # noqa: E402

TMP = os.environ.get("TMPDIR", "/tmp")
AUTO = "automation.gewohnheit_wohnzimmer_an"
DAY = 86400


class FakeSuggester:
    def name(self, entity_id):
        return "Gewohnheit: Wohnzimmer an"

    def phones_for(self, user_id):
        return ["mobile_app_pixel"]


class FakeHA:
    def __init__(self, store):
        self.store, self.suggester = store, FakeSuggester()
        self.states = {AUTO: {"state": "on", "attributes": {"id": "habit_learner_1"}}}
        self.calls, self.rests = [], []

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, data))
        if (domain, service) == ("automation", "turn_off"):
            self.states[data["entity_id"]]["state"] = "off"

    async def rest(self, method, path, payload=None):
        self.rests.append((method, path))


def run(store, ts, *, undo=None, home=True, attrs=None):
    """Ein Lauf der Automation, optional gefolgt von einer Hand-Aktion."""
    store.add(ts=ts, entity_id="light.wohnzimmer", old_state="off", new_state="on",
              attributes={"brightness": 128}, origin="automation", user_id=None,
              context_id=None, automation=AUTO,
              context={"home": ["person.alex"] if home else []})
    if undo:
        store.add(ts=ts + 120, entity_id="light.wohnzimmer", old_state="on",
                  new_state=undo, attributes=attrs, origin="manual", user_id="u",
                  context_id=None)


async def scenario(path):
    if os.path.exists(path):
        os.remove(path)
    store = Store(path)
    sid = store.add_suggestion(pattern="p", text="Wohnzimmer abends an", config={},
                               offered=0)
    store.answer_suggestion(sid, status="angenommen", answered=1000,
                            automation="habit_learner_1")
    ha = FakeHA(store)
    r = Reviewer(ha)
    t = 2000

    # Bewertung: gelassen, korrigiert, rückgängig, niemand da (zählt nicht)
    run(store, t)
    run(store, t + DAY, undo="on", attrs={"brightness": 255})
    run(store, t + 2 * DAY, undo="off")
    run(store, t + 3 * DAY, home=False)
    verdicts = [x["verdict"] for x in judge_runs(store.db, AUTO, 0)]
    assert verdicts == ["gelassen", "korrigiert", "rückgängig"], verdicts

    # Zwei Mal rückgängig → noch nicht pausiert
    await r.check(t + 4 * DAY)
    assert store.automated()[0]["status"] == "angenommen"

    # Drittes Mal rückgängig in den letzten fünf → pausiert und gemeldet
    run(store, t + 4 * DAY, undo="off")
    run(store, t + 5 * DAY, undo="off")
    now = t + 6 * DAY
    await r.check(now)
    s = store.automated()[0]
    assert s["status"] == "pausiert" and s["paused"] == now, s
    assert ("automation", "turn_off", {"entity_id": AUTO}) in ha.calls
    assert [c[1] for c in ha.calls if c[0] == "notify"] == ["mobile_app_pixel"]

    # Wieder eingeschaltet → angenommen, alte Läufe zählen nicht mehr
    ha.states[AUTO]["state"] = "on"
    await r.check(now + DAY)
    s = store.automated()[0]
    assert s["status"] == "angenommen" and s["since"] == now + DAY, s
    assert r.runs(s) == []

    # Erneut pausiert, 14 Tage nichts → gelöscht
    for i in range(3):
        run(store, now + (2 + i) * DAY, undo="off")
    await r.check(now + 5 * DAY)
    assert store.automated()[0]["status"] == "pausiert"
    await r.check(now + 5 * DAY + (DELETE_DAYS - 1) * DAY)
    assert ha.rests == []
    await r.check(now + 5 * DAY + DELETE_DAYS * DAY)
    assert ha.rests == [("DELETE", "/config/automation/config/habit_learner_1")], ha.rests
    assert store.automated() == []
    assert store.last_suggestion("p")[0] == "gelöscht"
    print("alle Prüfungen bestanden")


if __name__ == "__main__":
    asyncio.run(scenario(os.path.join(TMP, "habits_review.db")))
