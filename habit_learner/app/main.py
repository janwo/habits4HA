"""Habit Learner – Phase 1: Ereignisse erfassen.

Verbindet sich über die Supervisor-API mit dem Home-Assistant-WebSocket,
lauscht auf Zustandsänderungen und speichert sie mit ihrer Herkunft:

  manual     – von einem Menschen ausgelöst (App, Dashboard, Sprachassistent)
  automation – von einer Automation/einem Skript ausgelöst
  device     – direkt am Gerät (Schalter, Fernbedienung, Hersteller-App)
  system     – Neustart, Verfügbarkeit u. ä.
"""
import asyncio
import datetime as dt
import json
import logging
import os

import aiohttp

from store import Store

OPTIONS_FILE = "/data/options.json"
DB_FILE = "/data/habits.db"
WS_URL = "ws://supervisor/core/websocket"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

IGNORED_STATES = {"unavailable", "unknown", None}

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
        self.store = Store(DB_FILE)
        self.msg_id = 0

    def next_id(self) -> int:
        self.msg_id += 1
        return self.msg_id

    async def run(self):
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

                await ws.send_json({"id": self.next_id(), "type": "subscribe_events",
                                    "event_type": "state_changed"})
                async for raw in ws:
                    if raw.type != aiohttp.WSMsgType.TEXT:
                        break
                    data = raw.json()
                    if data.get("type") == "event":
                        self.handle(data["event"])

    def handle(self, event: dict):
        d = event.get("data", {})
        entity_id = d.get("entity_id", "")
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
        ts = dt.datetime.fromisoformat(event["time_fired"]).timestamp()
        self.store.add(ts=ts, entity_id=entity_id, old_state=old_s, new_state=new_s,
                       attributes=new_attrs, origin=origin,
                       user_id=ctx.get("user_id"), context_id=ctx.get("id"))
        log.debug("%-10s %s: %s → %s", origin, entity_id, old_s, new_s)

    async def maintenance(self):
        """Einmal täglich alte Daten löschen und Statistik loggen."""
        while True:
            removed = self.store.purge(self.retention_days)
            log.info("Datenbank: %s (%d alte Einträge gelöscht)",
                     self.store.stats(), removed)
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
