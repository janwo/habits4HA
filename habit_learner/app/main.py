"""Habit Learner – Phase 1: Ereignisse erfassen.

Verbindet sich über die Supervisor-API mit dem Home-Assistant-WebSocket,
lauscht auf Zustandsänderungen und speichert sie mit ihrer Herkunft:

  manual     – von einem Menschen ausgelöst (App, Dashboard, Sprachassistent)
  automation – von einer Automation/einem Skript ausgelöst
  device     – direkt am Gerät (Schalter, Fernbedienung, Hersteller-App)
  system     – Neustart, Verfügbarkeit u. ä.

Zu jeder Aktion werden Bereich, auslösende Automation und eine
Kontext-Momentaufnahme (Sonne, Anwesenheit, Medien) gespeichert. Änderungen
an Personen und `trigger_entities` landen als eigene Auslöser-Ereignisse.
"""
import asyncio
import datetime as dt
import json
import logging
import os
from collections import OrderedDict

import aiohttp

import analysis
from store import Store

OPTIONS_FILE = "/data/options.json"
DB_FILE = "/data/habits.db"
WS_URL = "ws://supervisor/core/websocket"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

IGNORED_STATES = {"unavailable", "unknown", None}
MEDIA_ACTIVE = {"on", "playing", "paused", "idle"}
MAX_CONTEXTS = 2000   # gemerkte Automations-Kontexte für die Zuordnung

log = logging.getLogger("habit_learner")


def load_options() -> dict:
    try:
        with open(OPTIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def classify(ctx: dict, old: str | None, new: str | None) -> str:
    """Bestimmt, wer eine Änderung ausgelöst hat – anhand des HA-Kontexts."""
    if old in IGNORED_STATES or new in IGNORED_STATES:
        return "system"
    if ctx.get("parent_id"):
        return "automation"          # Folge eines anderen Ereignisses
    if ctx.get("user_id"):
        return "manual"              # Mensch über HA (App, UI, Google, Assist)
    return "device"                  # z. B. Wandschalter oder Hersteller-App


class Learner:
    def __init__(self, opts: dict):
        self.domains = set(opts.get("domains", ["light", "switch", "scene"]))
        self.exclude_users = set(opts.get("exclude_users", []))
        self.retention_days = int(opts.get("retention_days", 180))
        self.trigger_entities = set(opts.get("trigger_entities", []))
        self.store = Store(DB_FILE)
        self.msg_id = 0
        self.states: dict[str, dict] = {}       # aktueller Zustand aller Entitäten
        self.areas: dict[str, str] = {}         # entity_id → area_id
        self.contexts: OrderedDict[str, str] = OrderedDict()  # context_id → automation

    def next_id(self) -> int:
        self.msg_id += 1
        return self.msg_id

    async def run(self):
        if not TOKEN:
            log.error("SUPERVISOR_TOKEN fehlt – Anmeldung an Home Assistant nicht möglich")
        while True:
            try:
                await self.connect()
            except Exception as err:  # noqa: BLE001 – immer neu verbinden
                log.warning("Verbindung verloren (%s), neuer Versuch in 10 s", err)
            await asyncio.sleep(10)

    async def connect(self):
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL, heartbeat=30) as ws:
                msg = await ws.receive_json()
                if msg.get("type") != "auth_required":
                    raise RuntimeError(f"Unerwartete Antwort: {msg}")
                await ws.send_json({"type": "auth", "access_token": TOKEN})
                msg = await ws.receive_json()
                if msg.get("type") != "auth_ok":
                    raise RuntimeError(f"Anmeldung fehlgeschlagen: {msg}")
                log.info("Mit Home Assistant verbunden (HA %s)", msg.get("ha_version"))

                await self.load_areas(ws)
                self.states = {s["entity_id"]: s
                               for s in await self.call(ws, "get_states")}
                self.snapshot_triggers()

                for event_type in ("state_changed", "automation_triggered",
                                   "script_started"):
                    await self.call(ws, "subscribe_events", event_type=event_type)
                async for raw in ws:
                    if raw.type != aiohttp.WSMsgType.TEXT:
                        break
                    data = raw.json()
                    if data.get("type") == "event":
                        self.dispatch(data["event"])

    async def call(self, ws, type_: str, **payload):
        """Anfrage senden und auf die passende Antwort warten (nur vor dem Abo)."""
        msg_id = self.next_id()
        await ws.send_json({"id": msg_id, "type": type_, **payload})
        while True:
            msg = await ws.receive_json()
            if msg.get("id") == msg_id:
                if not msg.get("success", True):
                    raise RuntimeError(f"{type_} fehlgeschlagen: {msg.get('error')}")
                return msg.get("result")

    async def load_areas(self, ws):
        """Bereich je Entität – eigener Bereich, sonst der des Geräts."""
        devices = {d["id"]: d.get("area_id")
                   for d in await self.call(ws, "config/device_registry/list")}
        self.areas = {}
        for e in await self.call(ws, "config/entity_registry/list"):
            area = e.get("area_id") or devices.get(e.get("device_id"))
            if area:
                self.areas[e["entity_id"]] = area
        log.info("Bereiche geladen: %d Entitäten zugeordnet", len(self.areas))

    def is_trigger(self, entity_id: str) -> bool:
        return entity_id.startswith("person.") or entity_id in self.trigger_entities

    def snapshot_triggers(self):
        """Ausgangszustand der Auslöser speichern, damit der Verlauf lückenlos ist."""
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        for entity_id, state in self.states.items():
            if self.is_trigger(entity_id):
                self.store.add(ts=now, entity_id=entity_id, old_state=None,
                               new_state=state.get("state"), attributes=None,
                               origin="system", user_id=None, context_id=None,
                               kind="trigger", area_id=self.areas.get(entity_id))

    def context_snapshot(self) -> dict:
        sun = self.states.get("sun.sun", {}).get("attributes", {})
        return {
            "sun_elevation": sun.get("elevation"),
            "sun_rising": sun.get("rising"),
            "home": sorted(e for e, s in self.states.items()
                           if e.startswith("person.") and s.get("state") == "home"),
            "media": sorted(e for e, s in self.states.items()
                            if e.startswith("media_player.")
                            and s.get("state") in MEDIA_ACTIVE),
        }

    def remember_context(self, event: dict):
        """Automation/Skript merken, damit Folgeänderungen zugeordnet werden können."""
        ctx_id = (event.get("context") or {}).get("id")
        entity_id = event.get("data", {}).get("entity_id")
        if ctx_id and entity_id:
            self.contexts[ctx_id] = entity_id
            while len(self.contexts) > MAX_CONTEXTS:
                self.contexts.popitem(last=False)

    def dispatch(self, event: dict):
        if event.get("event_type") in ("automation_triggered", "script_started"):
            self.remember_context(event)
            return
        d = event.get("data", {})
        entity_id = d.get("entity_id", "")
        # Kontext erst nach dem Speichern aktualisieren: er beschreibt die Lage davor.
        self.handle(event)
        if d.get("new_state"):
            self.states[entity_id] = d["new_state"]
        else:
            self.states.pop(entity_id, None)

    def handle(self, event: dict):
        d = event.get("data", {})
        entity_id = d.get("entity_id", "")
        if self.is_trigger(entity_id):
            self.handle_trigger(event)
            return
        if entity_id.split(".", 1)[0] not in self.domains:
            return

        old = (d.get("old_state") or {})
        new = (d.get("new_state") or {})
        old_s, new_s = old.get("state"), new.get("state")
        old_attrs, new_attrs = old.get("attributes", {}), new.get("attributes", {})

        # Nur echte Änderungen: Zustand oder relevante Attribute (Farbe, Helligkeit)
        if old_s == new_s and old_attrs == new_attrs:
            return

        ctx = new.get("context") or {}
        if ctx.get("user_id") in self.exclude_users:
            return

        origin = classify(ctx, old_s, new_s)
        automation = self.contexts.get(ctx.get("parent_id")) or self.contexts.get(ctx.get("id"))
        ts = dt.datetime.fromisoformat(event["time_fired"]).timestamp()
        self.store.add(ts=ts, entity_id=entity_id, old_state=old_s, new_state=new_s,
                       attributes=new_attrs, origin=origin,
                       user_id=ctx.get("user_id"), context_id=ctx.get("id"),
                       area_id=self.areas.get(entity_id), automation=automation,
                       context=self.context_snapshot())
        log.debug("%-10s %s: %s → %s%s", origin, entity_id, old_s, new_s,
                  f" ({automation})" if automation else "")

    def handle_trigger(self, event: dict):
        """Auslöser (Personen, Wecker, …): nur Zustandswechsel zählen."""
        d = event.get("data", {})
        entity_id = d["entity_id"]
        old_s = (d.get("old_state") or {}).get("state")
        new_s = (d.get("new_state") or {}).get("state")
        if old_s == new_s:
            return
        ctx = (d.get("new_state") or {}).get("context") or {}
        ts = dt.datetime.fromisoformat(event["time_fired"]).timestamp()
        self.store.add(ts=ts, entity_id=entity_id, old_state=old_s, new_state=new_s,
                       attributes=None, origin=classify(ctx, old_s, new_s),
                       user_id=ctx.get("user_id"), context_id=ctx.get("id"),
                       kind="trigger", area_id=self.areas.get(entity_id))
        log.debug("auslöser  %s: %s → %s", entity_id, old_s, new_s)

    async def maintenance(self):
        """Einmal täglich alte Daten löschen, Statistik loggen und Muster suchen."""
        while True:
            removed = self.store.purge(self.retention_days)
            log.info("Datenbank: %s (%d alte Einträge gelöscht)",
                     self.store.stats(), removed)
            try:
                lines = analysis.report(analysis.analyse(self.store.db))
                log.info("Analyse: %d Muster", len(lines))
                for line in lines:
                    log.info("%s", line)
            except Exception:  # noqa: BLE001 – Analyse darf die Erfassung nie stoppen
                log.exception("Analyse fehlgeschlagen")
            await asyncio.sleep(24 * 3600)


async def main():
    opts = load_options()
    logging.basicConfig(
        level=opts.get("log_level", "info").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    learner = Learner(opts)
    log.info("Habit Learner gestartet – beobachte: %s", ", ".join(sorted(learner.domains)))
    await asyncio.gather(learner.run(), learner.maintenance())


if __name__ == "__main__":
    asyncio.run(main())
