"""Mustererkennung: Zeit-, Sonnen-, Folge- und Kontextmuster.

Für jede wiederkehrende manuelle Aktion (Entität + vergröbertes Ergebnis)
wird geprüft, ob sie an einer Uhrzeit, einer Sonnenhöhe oder einem
vorausgehenden Auslöser (Wecker, Heimkommen, …) hängt. Gewinner ist der
Bezugspunkt mit der geringsten Streuung. Gezählt wird pro Gelegenheit (Tag
mit Anwesenheit bzw. Auftreten des Auslösers), nicht nur pro Ereignis.

Bereit ist ein Muster, wenn es mit ≥ 90 % Sicherheit an mehr als 60 % der
Gelegenheiten auftritt (Beta-Verteilung über Treffer und Fehlschläge) – so
zählen 5 von 5 und 30 von 50 unterschiedlich viel. Uhrzeiten werden als
Kreis gerechnet; mehrere Zeitpunkte am Tag ergeben getrennte Muster. Hat
sich eine Gewohnheit verschoben, zählt nur die Zeit seit der Verschiebung.

Zusätzlich für Lichter: Hängt die Einstellung beim Einschalten von der
Tageszeit ab (morgens warmweiß, abends rot), ist das ein Kontextmuster.
"""
import bisect
import datetime as dt
import json
import math
import os
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

WINDOW_DAYS = 56          # ausgewerteter Zeitraum
MERGE_SECONDS = 10        # schnelle Folgeänderungen = eine Aktion
MIN_RATE = 0.6           # Quote, die das Muster sicher übertreffen muss …
CONFIDENCE = 0.9         # … mit dieser Wahrscheinlichkeit
MAX_SPREAD = 20           # Minuten
MIN_TOLERANCE = 10        # Minuten – Mindestfenster für Treffer
MIN_PER_DEGREE = 4.5      # grobe Umrechnung Sonnenhöhe → Minuten
FOLLOW_MAX = 30           # Minuten – so lange darf eine Folgeaktion auf sich warten lassen
CONTEXT_MIN_SHARE = 0.7   # Anteil der dominanten Einstellung je Tageszeit
CONTEXT_MIN_ACTS = 3      # Einschaltvorgänge je Tageszeit, damit sie zählt
PARTS = (("morgens", 5, 11), ("tagsüber", 11, 17), ("abends", 17, 22), ("nachts", 22, 5))
CLUSTER_GAP = 120         # Minuten – getrennte Zeitpunkte am Tag
CLUSTER_SIGMA = 20        # Minuten – Glättung der Tagesverteilung
SHIFT_MIN = 15            # Minuten – ab hier gilt eine Verschiebung
SEGMENT_MIN = 5           # Tage je Abschnitt vor/nach einer Verschiebung
STATEFUL = ("person.", "binary_sensor.", "input_boolean.", "switch.", "device_tracker.")

DAY_GROUPS = {
    "täglich": set(range(7)),
    "werktags": {0, 1, 2, 3, 4},
    "am Wochenende": {5, 6},
}


@dataclass
class Pattern:
    key: str                  # z. B. "light.wohnzimmer → an 50 % warm"
    entity_id: str
    anchor: str               # "zeit" | "sonne" | "folge" | "kontext"
    days: str                 # Schlüssel aus DAY_GROUPS
    center: float             # Minuten seit Mitternacht, Sonnenhöhe in Grad
                              # bzw. Minuten nach dem Auslöser
    rising: bool | None       # nur bei Sonne: morgens (True) / abends (False)
    spread: float             # Minuten
    hits: int
    chances: int
    other_spread: float | None = None   # Streuung des unterlegenen Bezugspunkts
    automated: bool = False
    target_state: str | None = None     # konkreter Zielzustand – letzter Treffer
    target_attrs: dict = field(default_factory=dict)
    trigger: str | None = None          # Folgemuster: "input_datetime.wecker → geändert"
    settings: dict = field(default_factory=dict)  # Kontext: Tageszeit → key, attrs, share
    changed_at: float | None = None     # Gewohnheit hat sich hier verschoben

    @property
    def rate(self) -> float:
        return self.hits / self.chances if self.chances else 0.0

    @property
    def confidence(self) -> float:
        return confidence(self.hits, self.chances)

    @property
    def ready(self) -> bool:
        return (self.confidence >= CONFIDENCE and self.spread <= MAX_SPREAD
                and not self.automated)

    @property
    def unit(self) -> str:
        return "Tagen" if self.anchor in ("zeit", "sonne") else "Mal"

    def when(self) -> str:
        if self.anchor == "folge":
            return f"{round(self.center)} min nach {self.trigger}"
        if self.anchor == "kontext":
            return ", ".join(f"{part}: {s['key'].split(' → ', 1)[1]}"
                             for part, s in self.settings.items())
        if self.anchor == "zeit":
            m = round(self.center) % 1440
            return f"{self.days} {m // 60:02d}:{m % 60:02d}"
        part = "morgens" if self.rising else "abends"
        return f"{self.days} bei Sonnenhöhe {self.center:+.1f}° ({part})"

    def describe(self) -> str:
        text = (f"{self.key}  {self.when()}  – {self.hits} von {self.chances} {self.unit},"
                f" Streuung ±{self.spread:.0f} min")
        if self.other_spread is not None:
            other = "Uhrzeit" if self.anchor == "sonne" else "Sonne"
            text += f" ({other} hätte ±{self.other_spread:.0f} min)"
        if self.changed_at:
            text += f", verschoben seit {dt.datetime.fromtimestamp(self.changed_at):%d.%m.}"
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


def trigger_key(entity_id: str, state: str) -> str:
    """Auslöser vergröbern: Zustände bei Personen & Co., sonst nur „geändert“ (Wecker)."""
    if entity_id.startswith(STATEFUL) or state in ("on", "off", "home", "not_home"):
        return f"{entity_id} → {state}"
    return f"{entity_id} → geändert"


def part_of_day(minute: float) -> str:
    h = minute / 60
    for name, a, b in PARTS:
        if (a <= h < b) if a < b else (h >= a or h < b):
            return name
    return PARTS[-1][0]


def load_triggers(db: sqlite3.Connection, since: float) -> dict[str, list[float]]:
    """Echte Auslöser-Wechsel (ohne Ausgangszustände beim Verbinden)."""
    rows = db.execute(
        "SELECT ts, entity_id, new_state FROM events WHERE kind = 'trigger'"
        " AND origin != 'system' AND ts >= ? ORDER BY ts", (since,)).fetchall()
    out: dict[str, list[float]] = defaultdict(list)
    for ts, entity_id, state in rows:
        if state not in ("unavailable", "unknown", None):
            out[trigger_key(entity_id, state)].append(ts)
    return out


def load_actions(db: sqlite3.Connection, since: float, origins: tuple) -> list[dict]:
    rows = db.execute(
        "SELECT ts, entity_id, new_state, attributes, context, old_state FROM events"
        " WHERE kind = 'action' AND ts >= ? AND origin IN (%s) ORDER BY entity_id, ts"
        % ",".join("?" * len(origins)), (since, *origins)).fetchall()
    merged: list[dict] = []
    for ts, entity_id, state, attrs, ctx, old in rows:
        a = {"ts": ts, "entity_id": entity_id, "state": state, "old": old,
             "attrs": json.loads(attrs) if attrs else {},
             "ctx": json.loads(ctx) if ctx else {}}
        last = merged[-1] if merged else None
        if last and last["entity_id"] == entity_id and ts - last["ts"] <= MERGE_SECONDS:
            a["ctx"] = last["ctx"]          # Kontext vor der ersten Änderung
            a["old"] = last["old"]
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


def confidence(hits: int, chances: int, rate: float = MIN_RATE) -> float:
    """P(wahre Quote > rate) bei Beta(hits+1, misses+1) – exakt über die Binomialverteilung."""
    if chances <= 0:
        return 0.0
    n = chances + 1
    return sum(math.comb(n, k) * rate ** k * (1 - rate) ** (n - k)
               for k in range(min(hits, n) + 1))


def circular_mean(minutes: list[float]) -> float:
    angles = [m / 1440 * 2 * math.pi for m in minutes]
    a = math.atan2(sum(map(math.sin, angles)), sum(map(math.cos, angles)))
    return (a / (2 * math.pi) * 1440) % 1440


def unwrap(minutes: list[float]) -> tuple[list[float], float]:
    """Uhrzeiten so drehen, dass ihr Kreismittel bei 12:00 liegt – dann linear rechnen."""
    m0 = circular_mean(minutes)
    return [(m - m0 + 720) % 1440 for m in minutes], m0


def clock_center(minutes: list[float]) -> tuple[float, float]:
    """Median und Abweichung von Uhrzeiten, über Mitternacht hinweg."""
    vals, m0 = unwrap(minutes)
    center, spread = spread_of(vals)
    return (center + m0 - 720) % 1440, spread


def time_clusters(actions: list[dict], zone) -> list[list[dict]]:
    """Getrennte Zeitpunkte am Tag (z. B. morgens und abends) – Gipfel der geglätteten
    Tagesverteilung, mindestens CLUSTER_GAP auseinander."""
    if len(actions) < 6:
        return [actions]
    bins = 288
    hist = [0.0] * bins
    minutes = [minute_of_day(a["ts"], zone) for a in actions]
    for m in minutes:
        hist[int(m // 5) % bins] += 1
    sigma = CLUSTER_SIGMA / 5
    reach = int(3 * sigma)
    kernel = [math.exp(-(d * d) / (2 * sigma * sigma)) for d in range(-reach, reach + 1)]
    smooth = [sum(hist[(i + d) % bins] * kernel[d + reach] for d in range(-reach, reach + 1))
              for i in range(bins)]
    peaks = sorted((i for i in range(bins)
                    if smooth[i] > 0 and smooth[i] >= smooth[i - 1]
                    and smooth[i] > smooth[(i + 1) % bins]),
                   key=lambda i: -smooth[i])
    centers: list[float] = []
    for i in peaks:
        if all(clock_dist(i * 5, c) >= CLUSTER_GAP for c in centers):
            centers.append(i * 5)
    groups: list[list[dict]] = [[] for _ in centers]
    for a, m in zip(actions, minutes):
        groups[min(range(len(centers)), key=lambda j: clock_dist(m, centers[j]))].append(a)
    groups = [g for g in groups if len(g) >= max(3, len(actions) * 0.15)]
    return groups if len(groups) > 1 else [actions]


def change_point(values: list[float], days: list[dt.date]) -> int | None:
    """Index, ab dem sich eine Gewohnheit deutlich verschoben hat – oder None.
    Zwei Abschnitte mit je eigenem Median erklären die Werte viel besser als einer."""
    n = len(values)
    if n < 2 * SEGMENT_MIN:
        return None

    def cost(vs):
        c = statistics.median(vs)
        return sum(abs(v - c) for v in vs)

    total = cost(values)
    best = None
    for k in range(SEGMENT_MIN, n - SEGMENT_MIN + 1):
        c = cost(values[:k]) + cost(values[k:])
        if best is None or c < best[0]:
            best = (c, k)
    c, k = best
    shift = abs(statistics.median(values[k:]) - statistics.median(values[:k]))
    if shift < SHIFT_MIN or c > 0.5 * total:
        return None
    # Ein Sprung, kein Wandern: Jeder Abschnitt muss für sich stabil sein.
    # (Sonnenmuster wandern in Uhrzeit gemessen täglich ein paar Minuten.)
    if change_point(values[:k], days[:k]) is not None or \
            change_point(values[k:], days[k:]) is not None:
        return None
    return k


def clock_dist(a: float, b: float) -> float:
    d = abs(a - b) % 1440
    return min(d, 1440 - d)


def evaluate(actions: list[dict], days_window: list[dt.date], zone, presence: Presence,
             anchor: str, rising: bool | None, group: str,
             detect_shift: bool = True) -> Pattern | None:
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
    if anchor == "zeit":
        unwrapped, m0 = unwrap(vals)
        center = (statistics.median(unwrapped) + m0 - 720) % 1440
    else:
        center = statistics.median(vals)
    per_day: dict[dt.date, dict] = {}
    for a in acts:
        day = dt.datetime.fromtimestamp(a["ts"], zone).date()
        if day not in per_day or dist(value(a), center) < dist(value(per_day[day]), center):
            per_day[day] = a
    chosen = sorted(per_day.values(), key=lambda a: a["ts"])
    vals = [value(a) for a in chosen]
    if detect_shift:
        linear = (unwrap(vals)[0] if anchor == "zeit"
                  else [v * MIN_PER_DEGREE for v in vals])
        k = change_point(linear, sorted(per_day))
        if k is not None:
            since = chosen[k]["ts"]
            first_day = dt.datetime.fromtimestamp(since, zone).date()
            p = evaluate([a for a in actions if a["ts"] >= since],
                         [d for d in days_window if d >= first_day], zone, presence,
                         anchor, rising, group, detect_shift=False)
            if p:
                p.changed_at = since
            return p
    if anchor == "zeit":
        center, spread = clock_center(vals)
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


def follow_patterns(actions: list[dict], triggers: dict[str, list[float]],
                    presence: Presence) -> list[Pattern]:
    """Hängt die Aktion an einem vorausgehenden Auslöser? Je Auslöser ein Kandidat."""
    entity_id = actions[0]["entity_id"]
    times = [a["ts"] for a in actions]
    out = []
    for tkey, starts in triggers.items():
        if tkey.startswith(entity_id + " "):
            continue
        chances, delays = 0, []
        for t in starts:
            if presence.home_at(t + 60) is False:
                continue
            chances += 1
            i = bisect.bisect_right(times, t)
            if i < len(times) and times[i] - t <= FOLLOW_MAX * 60:
                delays.append(((times[i] - t) / 60, actions[i]))
        if len(delays) < 2:
            continue
        center, spread = spread_of([d for d, _ in delays])
        tolerance = max(2 * spread, MIN_TOLERANCE)
        hits = [a for d, a in delays if abs(d - center) <= tolerance]
        latest = max(hits, key=lambda a: a["ts"]) if hits else delays[-1][1]
        out.append(Pattern(key=actions[0]["key"], entity_id=entity_id, anchor="folge",
                           days="täglich", center=center, rising=None, spread=spread,
                           hits=len(hits), chances=chances, trigger=tkey,
                           target_state=latest["state"], target_attrs=latest["attrs"]))
    return out


def context_pattern(entity_id: str, actions: list[dict], zone) -> Pattern | None:
    """Licht: Einstellung beim Einschalten je nach Tageszeit."""
    ons = [a for a in actions if a["state"] == "on" and a["old"] != "on"]
    parts: dict[str, list] = defaultdict(list)
    for a in ons:
        parts[part_of_day(minute_of_day(a["ts"], zone))].append(a)
    settings, hits, chances = {}, 0, 0
    for name, *_ in PARTS:
        acts = parts.get(name, [])
        if len(acts) < CONTEXT_MIN_ACTS:
            continue
        key, n = Counter(a["key"] for a in acts).most_common(1)[0]
        if n / len(acts) < CONTEXT_MIN_SHARE:
            continue
        latest = max((a for a in acts if a["key"] == key), key=lambda a: a["ts"])
        settings[name] = {"key": key, "attrs": latest["attrs"], "share": round(n / len(acts), 2)}
        hits, chances = hits + n, chances + len(acts)
    if len({s["key"] for s in settings.values()}) < 2:
        return None                   # immer gleich – kein Kontext nötig
    return Pattern(key=f"{entity_id} → Einstellung je Tageszeit", entity_id=entity_id,
                   anchor="kontext", days="täglich", center=0, rising=None, spread=0,
                   hits=hits, chances=chances, target_state="on", settings=settings)


def best_pattern(actions: list[dict], days_window, zone, presence,
                 triggers: dict | None = None) -> Pattern | None:
    candidates = {}
    for p in follow_patterns(actions, triggers or {}, presence):
        candidates[("täglich", "folge", p.trigger)] = p
    for group in DAY_GROUPS:
        for anchor, rising in (("zeit", None), ("sonne", True), ("sonne", False)):
            p = evaluate(actions, days_window, zone, presence, anchor, rising, group)
            if p:
                candidates[(group, anchor, rising)] = p
    if not candidates:
        return None

    def score(p: Pattern):
        # Bereite Muster zuerst, dann höchste Trefferquote, dann geringste Streuung.
        return (p.ready, round(p.confidence, 2), -p.spread, p.hits)

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
    triggers = load_triggers(db, since)
    by_key: dict[str, list] = defaultdict(list)
    lights: dict[str, list] = defaultdict(list)
    for a in load_actions(db, since, ("manual", "device")):
        by_key[a["key"]].append(a)
        if a["entity_id"].startswith("light."):
            lights[a["entity_id"]].append(a)
    automated_keys = defaultdict(list)
    for a in load_actions(db, since, ("automation",)):
        automated_keys[a["key"]].append(a)

    patterns = []
    for key, actions in by_key.items():
        clusters = time_clusters(actions, zone)
        overall = best_pattern(actions, days_window, zone, presence, triggers)
        if len(clusters) == 1 or (overall and overall.anchor == "folge" and overall.ready):
            found = [(overall, actions)]
        else:                         # mehrere Zeitpunkte am Tag: je eigenes Muster
            found = [(best_pattern(c, days_window, zone, presence), c) for c in clusters]
        for p, acts in found:
            if not p:
                continue
            # Erledigt eine Automation dasselbe bereits zur selben Zeit?
            typical = clock_center([minute_of_day(x["ts"], zone) for x in acts])[0]
            auto = [a for a in automated_keys.get(key, [])
                    if clock_dist(minute_of_day(a["ts"], zone), typical) <= 60]
            if len(auto) >= max(3, p.chances // 2):
                p.automated = True
            patterns.append(p)
    for entity_id, actions in lights.items():
        if p := context_pattern(entity_id, actions, zone):
            patterns.append(p)
    patterns.sort(key=lambda p: (not p.ready, -p.confidence, p.spread))
    return patterns


def progress(p: Pattern) -> dict:
    """Wie nah ist ein Muster an den Schwellen? Je Kriterium 0…1, plus Gesamtwert."""
    parts = {
        "confidence": min(p.confidence / CONFIDENCE, 1.0),
        "spread": min(MAX_SPREAD / p.spread, 1.0) if p.spread else 1.0,
    }
    return {**parts, "total": sum(parts.values()) / len(parts)}


def status(p: Pattern) -> str:
    if p.automated:
        return "erledigt"
    if p.ready:
        return "bereit"
    if p.hits >= 2 and p.spread <= MAX_SPREAD * 1.5 and p.rate >= MIN_RATE / 2:
        return "beobachtet"
    return "schwach"


def report(patterns: list[Pattern]) -> list[str]:
    lines = []
    for p in patterns:
        if p.ready:
            lines.append(f"VORSCHLAG  {p.describe()}")
        elif p.automated:
            lines.append(f"ERLEDIGT   {p.describe()} – bereits automatisiert")
        elif status(p) == "beobachtet":
            hint = f", Sicherheit {p.confidence:.0%}"
            lines.append(f"BEOBACHTE  {p.describe()}{hint}")
    return lines
