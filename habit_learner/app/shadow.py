"""Schattenmodus – Phase 3: Vorhersagen live prüfen, ohne etwas zu schalten.

Für jedes bereite Muster öffnet sich zur erwarteten Zeit ein Fenster. Tust du
in diesem Fenster genau das Vorhergesagte, ist es ein Treffer; tust du an der
Entität etwas anderes, ist es abweichend; tust du nichts, verpasst. Ist die
Entität beim Öffnen schon im erwarteten Zustand, wird nichts vorhergesagt.

Folgemuster öffnen ihr Fenster beim Auslöser, Kontextmuster beim manuellen
Einschalten des Lichts (erwartet: die Einstellung der Tageszeit).

Ist für ein Muster der Autopilot frei, schaltet der Schattenmodus zur
erwarteten Mitte selbst (Rückruf `on_autopilot`) und wertet das Fenster als
„autopilot“ statt auf dich zu warten.
"""
import datetime as dt
import logging
from dataclasses import dataclass

from analysis import (DAY_GROUPS, MIN_PER_DEGREE, MIN_TOLERANCE, Pattern,
                      minute_of_day, part_of_day)

CONTEXT_WINDOW = 60       # Sekunden, um nach dem Einschalten fertig einzustellen

log = logging.getLogger("habit_learner")


def pattern_id(p: Pattern) -> str:
    """Stabil über Tage hinweg, auch wenn sich die Mitte leicht verschiebt."""
    if p.anchor == "sonne":
        anchor = f"sonne-{'auf' if p.rising else 'ab'}"
    elif p.anchor == "folge":
        anchor = f"folge-{p.trigger}"
    else:
        anchor = p.anchor
    return f"{p.key} | {anchor} | {p.days}"


@dataclass
class Prediction:
    pattern: Pattern
    db_id: int
    opened: float
    closes: float
    center_ts: float
    expected: str
    attrs: dict
    deviated: bool = False


class Shadow:
    def __init__(self, store, zone):
        self.store = store
        self.zone = zone
        self.patterns: list[Pattern] = []
        self.open: list[Prediction] = []
        self.done_today: dict[str, dt.date] = {}   # pattern_id → Tag der letzten Vorhersage
        self.on_hit = None                          # Rückruf (pattern_id, user_id, ts)
        self.autopilot: set[str] = set()            # pattern_ids, die selbst schalten
        self.on_autopilot = None                    # Rückruf (pattern, expected, attrs)

    def set_patterns(self, patterns: list[Pattern]):
        self.patterns = [p for p in patterns if p.ready]
        log.info("Schattenmodus: %d Muster aktiv", len(self.patterns))

    @staticmethod
    def tolerance(p: Pattern) -> float:
        return max(2 * p.spread, MIN_TOLERANCE)

    # ------------------------------------------------------------ öffnen

    def tick(self, now: float, states: dict, current_key):
        """Regelmäßig aufrufen: Zeitmuster öffnen, abgelaufene Fenster schließen."""
        local = dt.datetime.fromtimestamp(now, self.zone)
        minute = local.hour * 60 + local.minute + local.second / 60
        for p in self.patterns:
            if p.anchor != "zeit" or local.weekday() not in DAY_GROUPS[p.days]:
                continue
            start = (p.center - self.tolerance(p)) % 1440
            if 0 <= (minute - start) % 1440 < 2:      # Öffnungszeitpunkt erreicht
                center_ts = now + self.tolerance(p) * 60
                self.try_open(p, now, center_ts, states, current_key)
        for pred in list(self.open):
            self.maybe_act(pred, now, current_key)
        self.expire(now)

    def on_trigger(self, now: float, trigger: str, states, current_key):
        """Auslöser-Wechsel: Folgemuster öffnen."""
        for p in self.patterns:
            if p.anchor == "folge" and p.trigger == trigger:
                self.try_open(p, now, now + p.center * 60, states, current_key)

    def on_sun(self, now: float, old_elev, new_elev, rising, states, current_key):
        """Sonnenmuster öffnen, wenn die Sonne die Öffnungshöhe überschreitet."""
        if old_elev is None or new_elev is None:
            return
        local = dt.datetime.fromtimestamp(now, self.zone)
        for p in self.patterns:
            if p.anchor != "sonne" or p.rising != rising:
                continue
            if local.weekday() not in DAY_GROUPS[p.days]:
                continue
            # Fenster öffnet „tolerance“ Minuten vor der Mitte – in Grad umgerechnet.
            offset = self.tolerance(p) / MIN_PER_DEGREE
            start = p.center - offset if rising else p.center + offset
            crossed = (old_elev < start <= new_elev) if rising else (old_elev > start >= new_elev)
            if crossed:
                self.try_open(p, now, now + self.tolerance(p) * 60, states, current_key)

    def try_open(self, p: Pattern, now: float, center_ts: float, states, current_key,
                 expected: str | None = None, attrs: dict | None = None,
                 closes: float | None = None):
        pid = pattern_id(p)
        if any(pattern_id(x.pattern) == pid for x in self.open):
            return
        if p.anchor in ("zeit", "sonne"):
            today = dt.datetime.fromtimestamp(now, self.zone).date()
            if self.done_today.get(pid) == today:
                return
            self.done_today[pid] = today
        expected = expected or p.key
        if not any(e.startswith("person.") and s.get("state") == "home"
                   for e, s in states.items()):
            log.debug("SCHATTEN  %s – niemand zuhause, keine Vorhersage", p.key)
            return
        if current_key(p.entity_id) == expected:
            log.debug("SCHATTEN  %s – schon so, keine Vorhersage", expected)
            return
        closes = closes or center_ts + self.tolerance(p) * 60
        db_id = self.store.add_prediction(pattern=pid, entity_id=p.entity_id,
                                          expected=expected, opened=now, closes=closes)
        pred = Prediction(p, db_id, now, closes, center_ts, expected,
                          attrs if attrs is not None else p.target_attrs)
        self.open.append(pred)
        log.info("SCHATTEN  erwarte %s bis %s", expected,
                 dt.datetime.fromtimestamp(closes, self.zone).strftime("%H:%M"))
        self.maybe_act(pred, now, current_key)

    def maybe_act(self, pred: Prediction, now: float, current_key):
        """Autopilot: zur erwarteten Mitte selbst schalten."""
        if pattern_id(pred.pattern) not in self.autopilot or now < pred.center_ts:
            return
        if pred.pattern.anchor != "kontext" and current_key(pred.pattern.entity_id) == pred.expected:
            return                                   # schon erledigt – Fenster wertet es
        self.close(pred, now, "autopilot", None)
        if self.on_autopilot:
            self.on_autopilot(pred.pattern, pred.expected, pred.attrs)

    # ------------------------------------------------------------ auswerten

    def on_action(self, now: float, entity_id: str, key: str, user_id: str | None = None,
                  old_key: str | None = None, states=None, current_key=None):
        """Manuelle Aktion: passt sie zu einer offenen Vorhersage?"""
        if (old_key is not None and states is not None and " → an" not in old_key
                and " → an" in key):
            self.open_context(now, entity_id, states, current_key)
        for pred in list(self.open):
            if pred.pattern.entity_id != entity_id:
                continue
            if key == pred.expected:
                delay = (now - pred.center_ts) / 60
                self.close(pred, now, "treffer", delay)
                if self.on_hit:
                    self.on_hit(pattern_id(pred.pattern), user_id, now)
            else:
                pred.deviated = True       # z. B. Zwischenstufe beim Dimmen

    def open_context(self, now: float, entity_id: str, states, current_key):
        """Licht wurde von Hand eingeschaltet: Einstellung der Tageszeit erwarten."""
        part = part_of_day(minute_of_day(now, self.zone))
        for p in self.patterns:
            if p.anchor == "kontext" and p.entity_id == entity_id and part in p.settings:
                s = p.settings[part]
                self.try_open(p, now, now, states, lambda _e: None, expected=s["key"],
                              attrs=s["attrs"], closes=now + CONTEXT_WINDOW)

    def expire(self, now: float):
        for pred in list(self.open):
            if now >= pred.closes:
                self.close(pred, now, "abweichend" if pred.deviated else "verpasst", None)

    def close(self, pred: Prediction, now: float, result: str, delay):
        self.open.remove(pred)
        self.store.resolve_prediction(pred.db_id, result=result, resolved=now, delay=delay)
        extra = f" ({delay:+.0f} min)" if delay is not None else ""
        log.info("SCHATTEN  %-10s %s%s", result, pred.pattern.key, extra)

    def report(self, days: int = 28) -> list[str]:
        lines = []
        for pid, hits, total in self.store.prediction_stats(days):
            lines.append(f"SCHATTEN-BILANZ  {pid}: {hits} von {total} richtig")
        return lines
