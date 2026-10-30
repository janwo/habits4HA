"""Autopilot – experimentell, nur mit Option `autopilot` (D10).

Ein Muster wird freigegeben, wenn die letzten MIN_RECORD Vorhersagen im
Schattenmodus alle Treffer waren. Dann schaltet der Schattenmodus zur
erwarteten Zeit selbst, und jede Aktion kommt sofort mit „Rückgängig“ aufs
Handy. Schaltest du innerhalb von UNDO_MIN von Hand zurück, zählt das
ebenfalls als Rückgängig. Zweimal hintereinander rückgängig → zurück in den
Schattenmodus; die Freigabe muss dann neu verdient werden.

Nur Licht, Szenen und Medien – nie Schlösser, Alarm, Heizung oder Rollläden.
Was der Autopilot schaltet, ist als eigene Aktion markiert und wird nicht
mitgelernt.
"""
import asyncio
import logging
import time

from shadow import pattern_id
from suggest import ACTION_PREFIX, service_call

log = logging.getLogger("habit_learner")

ALLOWED = ("light.", "scene.", "media_player.")
MIN_RECORD = 10
UNDO_MIN = 15
UNDO_STREAK = 2
BLOCKED = {"angenommen", "abgelehnt", "pausiert", "gelöscht"}  # Antwort auf Vorschlag


class Autopilot:
    def __init__(self, learner, opts: dict):
        self.l = learner
        self.enabled = bool(opts.get("autopilot", False))

    # ------------------------------------------------------------ Freigabe

    def eligible(self, p) -> bool:
        pid = pattern_id(p)
        if not p.ready or not p.entity_id.startswith(ALLOWED):
            return False
        last = self.l.store.last_suggestion(pid)
        if last and last[0] in BLOCKED:
            return False                 # Automation angelegt oder ausdrücklich nein
        since = self.l.store.autopilot().get(pid, (None, 0))[1]
        record = self.l.store.last_predictions(pid, MIN_RECORD, since)
        return len(record) == MIN_RECORD and all(r == "treffer" for r in record)

    def refresh(self, patterns: list):
        """Nach Analyse und stündlich: neue Freigaben erteilen, Schatten informieren."""
        if not self.enabled:
            self.l.shadow.autopilot = set()
            return
        state = self.l.store.autopilot()
        now = time.time()
        for p in patterns:
            pid = pattern_id(p)
            if state.get(pid, ("",))[0] != "aktiv" and self.eligible(p):
                self.l.store.set_autopilot(pid, "aktiv", now)
                log.info("AUTOPILOT frei für %s", pid)
                self.notify("Autopilot übernimmt",
                            f"{self.l.suggester.name(p.entity_id)}: {MIN_RECORD} von "
                            f"{MIN_RECORD} richtig vorhergesagt – ab jetzt schalte ich selbst "
                            "und melde mich jedes Mal mit „Rückgängig“.")
        self.l.shadow.autopilot = {pid for pid, (s, _) in self.l.store.autopilot().items()
                                   if s == "aktiv"}

    # ------------------------------------------------------------ Schalten

    def on_act(self, p, expected: str, attrs: dict):
        """Vom Schattenmodus zur erwarteten Zeit aufgerufen."""
        asyncio.get_running_loop().create_task(self.act(p, expected, attrs))

    async def act(self, p, expected: str, attrs: dict):
        state = "on" if p.anchor == "kontext" else p.target_state
        call = service_call(p.entity_id, state, attrs)
        if not call:
            return
        prev = self.l.states.get(p.entity_id, {})
        prev = {"state": prev.get("state"), "attributes": prev.get("attributes", {})}
        domain, service = call["action"].split(".")
        await self.l.call_service(domain, service, {**call["target"], **call.get("data", {})})
        rid = self.l.store.add_autopilot_run(pattern=pattern_id(p), entity_id=p.entity_id,
                                             ts=time.time(), expected=expected, prev=prev)
        what = expected.split(" → ", 1)[1]
        log.info("AUTOPILOT  %s", expected)
        self.notify("Automatisch geschaltet",
                    f"{self.l.suggester.name(p.entity_id)}: {what}",
                    [{"action": f"{ACTION_PREFIX}UNDO_{rid}", "title": "Rückgängig"}],
                    tag=f"habit_autopilot_{rid}")

    # ------------------------------------------------------------ Rückgängig

    def on_notification_action(self, event: dict):
        action = (event.get("data") or {}).get("action", "")
        prefix = f"{ACTION_PREFIX}UNDO_"
        if action.startswith(prefix) and action[len(prefix):].isdigit():
            asyncio.get_running_loop().create_task(self.undo(int(action[len(prefix):])))

    async def undo(self, rid: int, restore: bool = True):
        run = self.l.store.autopilot_run(rid)
        if not run or run["result"]:
            return
        self.l.store.undo_autopilot_run(rid)
        prev = run["prev"] or {}
        if restore and prev.get("state"):
            call = service_call(run["entity_id"], prev["state"], prev.get("attributes", {}))
            if call:
                domain, service = call["action"].split(".")
                await self.l.call_service(domain, service,
                                          {**call["target"], **call.get("data", {})})
        log.info("AUTOPILOT  rückgängig: %s", run["expected"])
        results = self.l.store.autopilot_results(run["pattern"], UNDO_STREAK)
        if len(results) == UNDO_STREAK and all(r == "rückgängig" for r in results):
            self.l.store.set_autopilot(run["pattern"], "aus", time.time())
            self.l.shadow.autopilot.discard(run["pattern"])
            log.info("AUTOPILOT  aus für %s – zurück im Schattenmodus", run["pattern"])
            self.notify("Autopilot aus",
                        f"{self.l.suggester.name(run['entity_id'])} wurde zweimal "
                        "rückgängig gemacht – ich beobachte wieder nur.")

    def on_manual(self, now: float, entity_id: str, key: str):
        """Hand-Aktion kurz nach dem Autopiloten, die etwas anderes will = Rückgängig."""
        run = self.l.store.recent_autopilot_run(entity_id, now - UNDO_MIN * 60)
        if run and key != run["expected"]:
            asyncio.get_running_loop().create_task(self.undo(run["id"], restore=False))

    # ------------------------------------------------------------ Hilfen

    def notify(self, title: str, message: str, actions=None, tag: str | None = None):
        data = {}
        if actions:
            data["actions"] = actions
        if tag:
            data["tag"] = tag
        for phone in self.l.suggester.phones_for(None):
            asyncio.get_running_loop().create_task(self.l.call_service(
                "notify", phone, {"title": title, "message": message, "data": data}))
