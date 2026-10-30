"""Vorschlag im Moment – Phase 4.

Trifft der Schattenmodus ein bereites Muster, wird es direkt nach deiner
Aktion vorgeschlagen: Benachrichtigung aufs Handy mit Übernehmen / Nein danke /
Später, optional eine kurze Ansage über einen Lautsprecher. Übernehmen legt
die Automation in Home Assistant an.

Handys, Lautsprecher und Personen werden aus Home Assistant erkannt – es
muss nichts eingetragen werden.
"""
import asyncio
import datetime as dt
import logging
import time

from analysis import DAY_GROUPS, PARTS, Pattern
from shadow import pattern_id

log = logging.getLogger("habit_learner")

ACTION_PREFIX = "HABIT_"
LATER_DAYS = 3
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SERVICES = {   # Domain → (Zielzustand → Dienst)
    "light": {"on": "turn_on", "off": "turn_off"},
    "switch": {"on": "turn_on", "off": "turn_off"},
    "input_boolean": {"on": "turn_on", "off": "turn_off"},
    "fan": {"on": "turn_on", "off": "turn_off"},
    "media_player": {"on": "turn_on", "off": "turn_off"},
    "cover": {"open": "open_cover", "closed": "close_cover"},
}
LIGHT_ATTRS = ("brightness", "color_temp_kelvin")


def service_call(entity_id: str, state: str, attrs: dict) -> dict | None:
    """Dienstaufruf, der eine Entität in den Zustand bringt – Licht mit Helligkeit/Farbe."""
    domain = entity_id.split(".", 1)[0]
    service = SERVICES.get(domain, {}).get(state)
    if domain == "scene":
        service = "turn_on"
    if not service:
        return None
    data = {}
    if domain == "light" and state == "on":
        data = {k: attrs[k] for k in LIGHT_ATTRS if k in attrs}
        if "color_temp_kelvin" not in data and attrs.get("hs_color"):
            data["hs_color"] = attrs["hs_color"]
    action = {"action": f"{domain}.{service}", "target": {"entity_id": entity_id}}
    if data:
        action["data"] = data
    return action


def part_condition(part: str) -> dict:
    _, a, b = next(x for x in PARTS if x[0] == part)
    return {"condition": "time", "after": f"{a:02d}:00:00", "before": f"{b:02d}:00:00"}


def build_automation(p: Pattern, name: str) -> dict | None:
    """Automation, die das Muster umsetzt – None, wenn es sich nicht abbilden lässt."""
    conditions = [{"condition": "numeric_state", "entity_id": "zone.home", "above": 0}]
    base = {"alias": f"Gewohnheit: {name} {describe_action(p)}",
            "description": f"Angelegt vom Habit Learner – {p.describe()}",
            "mode": "single"}
    if p.anchor == "kontext":
        choose = []
        for part, setting in p.settings.items():
            action = service_call(p.entity_id, "on", setting["attrs"])
            if action:
                choose.append({"conditions": [part_condition(part)], "sequence": [action]})
        if not choose:
            return None
        return {**base,
                "triggers": [{"trigger": "state", "entity_id": p.entity_id,
                              "from": "off", "to": "on"}],
                "conditions": conditions, "actions": [{"choose": choose}]}
    action = service_call(p.entity_id, p.target_state, p.target_attrs)
    if not action:
        return None
    if p.anchor == "folge":
        entity, _, state = p.trigger.partition(" → ")
        trigger = {"trigger": "state", "entity_id": entity}
        if state != "geändert":
            trigger["to"] = state
        actions = [action]
        if round(p.center) >= 1:
            actions.insert(0, {"delay": {"minutes": round(p.center)}})
        return {**base, "triggers": [trigger], "conditions": conditions,
                "actions": actions, "mode": "restart"}
    if p.anchor == "zeit":
        m = round(p.center) % 1440
        trigger = {"trigger": "time", "at": f"{m // 60:02d}:{m % 60:02d}:00"}
    else:
        trigger = {"trigger": "numeric_state", "entity_id": "sun.sun",
                   "attribute": "elevation",
                   ("above" if p.rising else "below"): round(p.center, 1)}
    if p.days != "täglich":
        conditions.append({"condition": "time",
                           "weekday": [WEEKDAYS[d] for d in sorted(DAY_GROUPS[p.days])]})
    return {**base, "triggers": [trigger], "conditions": conditions, "actions": [action]}


def describe_action(p: Pattern) -> str:
    return p.key.split(" → ", 1)[1]


def describe_when(p: Pattern) -> str:
    if p.anchor == "folge":
        entity, _, state = p.trigger.partition(" → ")
        what = "geändert wird" if state == "geändert" else f"auf „{state}“ wechselt"
        return f"etwa {round(p.center)} min nachdem {entity} {what}"
    days = "" if p.days == "täglich" else f"{p.days} "
    if p.anchor == "zeit":
        m = round(p.center) % 1440
        return f"{days}gegen {m // 60:02d}:{m % 60:02d}"
    return f"{days}{'morgens bei Sonnenaufgang' if p.rising else 'abends bei Dämmerung'}"


def in_quiet_hours(minute: float, start: str, end: str) -> bool:
    def m(s):
        h, mi = s.split(":")
        return int(h) * 60 + int(mi)
    a, b = m(start), m(end)
    return a <= minute < b if a < b else minute >= a or minute < b


class Suggester:
    def __init__(self, learner, opts: dict):
        self.l = learner
        self.announce = bool(opts.get("announce", False))
        self.quiet_start = opts.get("quiet_start", "22:00")
        self.quiet_end = opts.get("quiet_end", "07:00")
        self.notify: set[str] = set()       # notify.mobile_app_* – erkannt
        self.patterns: dict[str, Pattern] = {}

    # ------------------------------------------------------------ Erkennung

    async def discover(self):
        services = await self.l.request("get_services")
        self.notify = {s for s in (services or {}).get("notify", {})
                       if s.startswith("mobile_app_")}
        log.info("Vorschläge: %d Handys, %d Lautsprecher, Ansage %s",
                 len(self.notify), len(self.players()), "an" if self.announce else "aus")

    def players(self) -> list[str]:
        return [e for e, p in self.l.platforms.items()
                if p == "cast" and e.startswith("media_player.")]

    def phones_for(self, user_id: str | None) -> list[str]:
        """Handy der Person, die gehandelt hat – sonst aller, die zuhause sind."""
        persons = {e: s for e, s in self.l.states.items() if e.startswith("person.")}
        chosen = [s for s in persons.values()
                  if user_id and s.get("attributes", {}).get("user_id") == user_id]
        if not chosen:
            chosen = [s for s in persons.values() if s.get("state") == "home"]
        phones = []
        for s in chosen:
            for tracker in s.get("attributes", {}).get("device_trackers", []):
                service = f"mobile_app_{tracker.split('.', 1)[1]}"
                if service in self.notify:
                    phones.append(service)
        return sorted(set(phones)) or sorted(self.notify)

    def name(self, entity_id: str) -> str:
        attrs = self.l.states.get(entity_id, {}).get("attributes", {})
        return attrs.get("friendly_name") or entity_id

    # ------------------------------------------------------------ Angebot

    def set_patterns(self, patterns: list[Pattern]):
        self.patterns = {pattern_id(p): p for p in patterns if p.ready}

    def on_hit(self, pid: str, user_id: str | None, now: float):
        """Vom Schattenmodus nach einem Treffer aufgerufen."""
        p = self.patterns.get(pid)
        if p and self.may_offer(pid, now):
            asyncio.get_running_loop().create_task(self.offer(pid, p, user_id, now))

    def may_offer(self, pid: str, now: float) -> bool:
        last = self.l.store.last_suggestion(pid)
        if last:
            status, when = last
            if status != "später" or now - when < LATER_DAYS * 86400:
                return False
        local = dt.datetime.fromtimestamp(now, self.l.zone)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.l.store.offers_since(midnight) == 0      # höchstens einer pro Tag

    async def offer(self, pid: str, p: Pattern, user_id: str | None, now: float):
        name = self.name(p.entity_id)
        config = build_automation(p, name)
        if not config:
            log.debug("Vorschlag %s lässt sich nicht als Automation abbilden", pid)
            return
        if p.anchor == "kontext":
            text = (f"Du stellst {name} beim Einschalten je nach Tageszeit ein ({p.when()}) – "
                    f"{p.hits} von {p.chances} Mal. Soll ich das künftig automatisch machen?")
        else:
            text = (f"Du schaltest {name} {describe_when(p)} {describe_action(p)} – "
                    f"{p.hits} von {p.chances} {'Tagen' if p.unit == 'Tagen' else 'Mal'}. "
                    f"Soll ich das künftig automatisch machen?")
        sid = self.l.store.add_suggestion(pattern=pid, text=text, config=config, offered=now)
        log.info("VORSCHLAG gesendet: %s", text)
        actions = [{"action": f"{ACTION_PREFIX}{a}_{sid}", "title": t}
                   for a, t in (("ACCEPT", "Übernehmen"), ("DECLINE", "Nein danke"),
                                ("LATER", "Später"))]
        for phone in self.phones_for(user_id):
            await self.l.call_service("notify", phone, {
                "title": "Gewohnheit erkannt", "message": text,
                "data": {"actions": actions, "tag": f"habit_{sid}"}})
        await self.maybe_announce(p, name, now)

    async def maybe_announce(self, p: Pattern, name: str, now: float):
        if not self.announce:
            return
        local = dt.datetime.fromtimestamp(now, self.l.zone)
        if in_quiet_hours(local.hour * 60 + local.minute, self.quiet_start, self.quiet_end):
            return
        players = self.players()
        area = self.l.areas.get(p.entity_id)
        same_area = [e for e in players if area and self.l.areas.get(e) == area]
        target = (same_area or players or [None])[0]
        tts = next((e for e in self.l.states if e.startswith("tts.")), None)
        if not target or not tts:
            return
        if self.l.states.get(target, {}).get("state") == "playing":
            return                               # nicht in laufende Medien sprechen
        await self.l.call_service("tts", "speak", {
            "entity_id": tts, "media_player_entity_id": target,
            "message": f"Ich hätte einen Vorschlag für {name}. Schau mal aufs Handy."})

    # ------------------------------------------------------------ Antwort

    def on_notification_action(self, event: dict):
        action = (event.get("data") or {}).get("action", "")
        if not action.startswith(ACTION_PREFIX):
            return
        kind, _, sid = action[len(ACTION_PREFIX):].partition("_")
        if sid.isdigit():
            asyncio.get_running_loop().create_task(self.answer(kind, int(sid)))

    async def answer(self, kind: str, sid: int):
        s = self.l.store.suggestion(sid)
        if not s or s["status"] != "offen":
            return
        now = time.time()
        if kind == "DECLINE":
            self.l.store.answer_suggestion(sid, status="abgelehnt", answered=now)
            log.info("Vorschlag %d abgelehnt", sid)
        elif kind == "LATER":
            self.l.store.answer_suggestion(sid, status="später", answered=now)
            log.info("Vorschlag %d später", sid)
        elif kind == "ACCEPT":
            automation_id = f"habit_learner_{sid}"
            await self.l.rest("POST", f"/config/automation/config/{automation_id}",
                              s["config"])
            self.l.store.answer_suggestion(sid, status="angenommen", answered=now,
                                           automation=automation_id)
            log.info("Vorschlag %d angenommen – Automation %s angelegt", sid, automation_id)
