"""Mustererkennung – Phase 2: Zeit- und Sonnenmuster.

Für jede wiederkehrende manuelle Aktion (Entität + vergröbertes Ergebnis)
wird geprüft, ob sie eher an einer Uhrzeit oder an einer Sonnenhöhe hängt.
Gewinner ist der Bezugspunkt mit der geringsten Streuung. Gezählt wird pro
Gelegenheit (Tag, an dem jemand zuhause war), nicht nur pro Ereignis.
"""
import datetime as dt
import json
import os
import sqlite3
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

WINDOW_DAYS = 56          # ausgewerteter Zeitraum
MERGE_SECONDS = 10        # schnelle Folgeänderungen = eine Aktion
MIN_HITS = 5
MIN_RATE = 0.6
MAX_SPREAD = 20           # Minuten
MIN_TOLERANCE = 10        # Minuten – Mindestfenster für Treffer
MIN_PER_DEGREE = 4.5      # grobe Umrechnung Sonnenhöhe → Minuten

DAY_GROUPS = {
    "täglich": set(range(7)),
    "werktags": {0, 1, 2, 3, 4},
    "am Wochenende": {5, 6},
}


@dataclass
class Pattern:
    key: str                  # z. B. "light.wohnzimmer → an 50 % warm"
    entity_id: str
    anchor: str               # "zeit" | "sonne"
    days: str                 # Schlüssel aus DAY_GROUPS
    center: float             # Minuten seit Mitternacht bzw. Sonnenhöhe in Grad
    rising: bool | None       # nur bei Sonne: morgens (True) / abends (False)
    spread: float             # Minuten
    hits: int
    chances: int
    other_spread: float | None = None   # Streuung des unterlegenen Bezugspunkts
    automated: bool = False
    target_state: str | None = None     # konkreter Zielzustand – letzter Treffer
    target_attrs: dict = field(default_factory=dict)

    @property
    def rate(self) -> float:
        return self.hits / self.chances if self.chances else 0.0

    @property
    def ready(self) -> bool:
        return (self.hits >= MIN_HITS and self.rate >= MIN_RATE
                and self.spread <= MAX_SPREAD and not self.automated)

    def when(self) -> str:
        if self.anchor == "zeit":
            m = round(self.center) % 1440
            return f"{self.days} {m // 60:02d}:{m % 60:02d}"
        part = "morgens" if self.rising else "abends"
        return f"{self.days} bei Sonnenhöhe {self.center:+.1f}° ({part})"

    def describe(self) -> str:
        text = (f"{self.key}  {self.when()}  – {self.hits} von {self.chances} Tagen,"
                f" Streuung ±{self.spread:.0f} min")
        if self.other_spread is not None:
            other = "Sonne" if self.anchor == "zeit" else "Uhrzeit"
            text += f" ({other} hätte ±{self.other_spread:.0f} min)"
        return text


# ---------------------------------------------------------------- Vorbereitung

def tz() -> dt.tzinfo:
    name = os.environ.get("TZ")
    return ZoneInfo(name) if name else dt.datetime.now().astimezone().tzinfo


def outcome(entity_id: str, state: str, attrs: dict) -> str:
    """Vergröbertes Ergebnis einer Aktion."""
    if state != "on" or not entity_id.startswith("light."):
        return {"on": "an", "off": "aus"}.get(state, state)
    parts = ["an"]
    if (b := attrs.get("brightness")) is not None:
        parts.append(f"{round(b / 255 * 4) * 25} %")
    if attrs.get("hs_color") and attrs.get("color_mode") in ("hs", "xy", "rgb"):
        parts.append("farbig")
    elif (k := attrs.get("color_temp_kelvin")) is not None:
        parts.append("warm" if k < 3300 else "neutral" if k < 5000 else "kalt")
    return " ".join(parts)


def load_actions(db: sqlite3.Connection, since: float, origins: tuple) -> list[dict]:
    rows = db.execute(
        "SELECT ts, entity_id, new_state, attributes, context FROM events"
        " WHERE kind = 'action' AND ts >= ? AND origin IN (%s) ORDER BY entity_id, ts"
        % ",".join("?" * len(origins)), (since, *origins)).fetchall()
    merged: list[dict] = []
    for ts, entity_id, state, attrs, ctx in rows:
        a = {"ts": ts, "entity_id": entity_id, "state": state,
             "attrs": json.loads(attrs) if attrs else {},
             "ctx": json.loads(ctx) if ctx else {}}
        last = merged[-1] if merged else None
        if last and last["entity_id"] == entity_id and ts - last["ts"] <= MERGE_SECONDS:
            a["ctx"] = last["ctx"]          # Kontext vor der ersten Änderung
            merged[-1] = a
        else:
            merged.append(a)
    for a in merged:
        a["key"] = f"{a['entity_id']} → {outcome(a['entity_id'], a['state'], a['attrs'])}"
    return merged


class Presence:
    """Rekonstruiert aus den Auslöser-Ereignissen, wer wann zuhause war."""

    def __init__(self, db: sqlite3.Connection):
        rows = db.execute(
            "SELECT ts, entity_id, new_state FROM events"
            " WHERE kind = 'trigger' AND entity_id LIKE 'person.%' ORDER BY ts").fetchall()
        self.timeline: dict[str, list[tuple[float, bool]]] = defaultdict(list)
        for ts, entity_id, state in rows:
            self.timeline[entity_id].append((ts, state == "home"))

    def home_at(self, ts: float) -> bool | None:
        """True/False – oder None, wenn nichts über Personen bekannt ist."""
        if not self.timeline:
            return None
        known = False
        for changes in self.timeline.values():
            state = None
            for t, home in changes:
                if t > ts:
                    break
                state = home
            if state:
                return True
            known = known or state is not None
        return False if known else None


# ---------------------------------------------------------------- Statistik

def minute_of_day(ts: float, zone) -> float:
    d = dt.datetime.fromtimestamp(ts, zone)
    return d.hour * 60 + d.minute + d.second / 60


def spread_of(values: list[float]) -> tuple[float, float]:
    """Median und mittlere absolute Abweichung."""
    center = statistics.median(values)
    return center, statistics.median(abs(v - center) for v in values)


def clock_center(minutes: list[float]) -> tuple[float, float, int]:
    """Wie spread_of, aber über Mitternacht hinweg (zweite Variante um 12 h verschoben)."""
    best = None
    for shift in (0, 720):
        vals = [(m + shift) % 1440 for m in minutes]
        center, spread = spread_of(vals)
        if best is None or spread < best[1]:
            best = ((center - shift) % 1440, spread, shift)
    return best


def clock_dist(a: float, b: float) -> float:
    d = abs(a - b) % 1440
    return min(d, 1440 - d)


def evaluate(actions: list[dict], days_window: list[dt.date], zone, presence: Presence,
             anchor: str, rising: bool | None, group: str) -> Pattern | None:
    weekdays = DAY_GROUPS[group]
    acts = [a for a in actions
            if dt.datetime.fromtimestamp(a["ts"], zone).weekday() in weekdays]
    if anchor == "sonne":
        acts = [a for a in acts if a["ctx"].get("sun_elevation") is not None
                and a["ctx"].get("sun_rising") == rising]
    if len(acts) < 2:
        return None

    def value(a):
        return (a["ctx"]["sun_elevation"] if anchor == "sonne"
                else minute_of_day(a["ts"], zone))

    def dist(v, c):
        return (abs(v - c) * MIN_PER_DEGREE if anchor == "sonne" else clock_dist(v, c))

    # Erst grob zentrieren, dann pro Tag nur das Ereignis nahe der Mitte werten.
    vals = [value(a) for a in acts]
    center = clock_center(vals)[0] if anchor == "zeit" else statistics.median(vals)
    per_day: dict[dt.date, dict] = {}
    for a in acts:
        day = dt.datetime.fromtimestamp(a["ts"], zone).date()
        if day not in per_day or dist(value(a), center) < dist(value(per_day[day]), center):
            per_day[day] = a
    chosen = list(per_day.values())
    vals = [value(a) for a in chosen]
    if anchor == "zeit":
        center, spread, _ = clock_center(vals)
    else:
        center, spread = spread_of(vals)
        spread *= MIN_PER_DEGREE
    tolerance = max(2 * spread, MIN_TOLERANCE)

    hit_days = {d for d, a in per_day.items() if dist(value(a), center) <= tolerance}
    # Gelegenheiten: passende Wochentage, an denen zur typischen Zeit jemand da war.
    typical = statistics.median(minute_of_day(a["ts"], zone) for a in chosen)
    chances = 0
    for day in days_window:
        if day.weekday() not in weekdays:
            continue
        moment = dt.datetime.combine(day, dt.time(), zone) + dt.timedelta(minutes=typical)
        home = presence.home_at(moment.timestamp())
        if home is not False or day in hit_days:
            chances += 1

    latest = max((per_day[d] for d in hit_days), key=lambda a: a["ts"], default=chosen[-1])
    return Pattern(key=chosen[0]["key"], entity_id=chosen[0]["entity_id"], anchor=anchor,
                   days=group, center=center, rising=rising, spread=spread,
                   hits=len(hit_days), chances=chances,
                   target_state=latest["state"], target_attrs=latest["attrs"])


def best_pattern(actions: list[dict], days_window, zone, presence) -> Pattern | None:
    candidates = {}
    for group in DAY_GROUPS:
        for anchor, rising in (("zeit", None), ("sonne", True), ("sonne", False)):
            p = evaluate(actions, days_window, zone, presence, anchor, rising, group)
            if p:
                candidates[(group, anchor, rising)] = p
    if not candidates:
        return None

    def score(p: Pattern):
        # Bereite Muster zuerst, dann höchste Trefferquote, dann geringste Streuung.
        return (p.ready, round(p.rate, 1), -p.spread, p.hits)

    best = max(candidates.values(), key=score)
    # Eine Einschränkung auf Werktage/Wochenende muss sich deutlich lohnen,
    # sonst gilt das Muster täglich.
    daily = candidates.get(("täglich", best.anchor, best.rising))
    if daily and best.days != "täglich" and best.rate - daily.rate < 0.15:
        best = daily
    others = [p.spread for (g, a, _), p in candidates.items()
              if g == best.days and a != best.anchor]
    best.other_spread = min(others) if others else None
    return best


# ---------------------------------------------------------------- Einstieg

def analyse(db: sqlite3.Connection, now: float | None = None, zone=None) -> list[Pattern]:
    now = now or time.time()
    zone = zone or tz()
    since = now - WINDOW_DAYS * 86400
    first = db.execute("SELECT MIN(ts) FROM events WHERE ts >= ?", (since,)).fetchone()[0]
    if first is None:
        return []
    start = dt.datetime.fromtimestamp(first, zone).date()
    end = dt.datetime.fromtimestamp(now, zone).date()
    # Der laufende Tag ist noch nicht vorbei und zählt nicht als Gelegenheit.
    days_window = [start + dt.timedelta(days=i) for i in range((end - start).days)]

    presence = Presence(db)
    by_key: dict[str, list] = defaultdict(list)
    for a in load_actions(db, since, ("manual", "device")):
        by_key[a["key"]].append(a)
    automated_keys = defaultdict(list)
    for a in load_actions(db, since, ("automation",)):
        automated_keys[a["key"]].append(a)

    patterns = []
    for key, actions in by_key.items():
        p = best_pattern(actions, days_window, zone, presence)
        if not p:
            continue
        # Erledigt eine Automation dasselbe bereits zur selben Zeit?
        auto = [a for a in automated_keys.get(key, [])
                if clock_dist(minute_of_day(a["ts"], zone),
                              statistics.median(minute_of_day(x["ts"], zone)
                                                for x in actions)) <= 60]
        if len(auto) >= max(3, p.chances // 2):
            p.automated = True
        patterns.append(p)
    patterns.sort(key=lambda p: (not p.ready, -p.rate, p.spread))
    return patterns


def progress(p: Pattern) -> dict:
    """Wie nah ist ein Muster an den Schwellen? Je Kriterium 0…1, plus Gesamtwert."""
    parts = {
        "hits": min(p.hits / MIN_HITS, 1.0),
        "rate": min(p.rate / MIN_RATE, 1.0),
        "spread": min(MAX_SPREAD / p.spread, 1.0) if p.spread else 1.0,
    }
    return {**parts, "total": sum(parts.values()) / len(parts)}


def status(p: Pattern) -> str:
    if p.automated:
        return "erledigt"
    if p.ready:
        return "bereit"
    return "beobachtet" if p.hits >= 2 and p.spread <= MAX_SPREAD * 1.5 else "schwach"


def report(patterns: list[Pattern]) -> list[str]:
    lines = []
    for p in patterns:
        if p.ready:
            lines.append(f"VORSCHLAG  {p.describe()}")
        elif p.automated:
            lines.append(f"ERLEDIGT   {p.describe()} – bereits automatisiert")
        elif status(p) == "beobachtet":
            missing = max(MIN_HITS - p.hits, 0)
            hint = f", noch ~{missing} Treffer" if missing else ""
            lines.append(f"BEOBACHTE  {p.describe()}{hint}")
    return lines
