"""Oberfläche in der Seitenleiste (Ingress) und JSON-API dafür.

Nur der Ingress-Proxy des Supervisors darf zugreifen; Home Assistant
übernimmt Anmeldung und Rechte.
"""
import logging
import os
import time

from aiohttp import web

import analysis
import review
from shadow import pattern_id

log = logging.getLogger("habit_learner")

PORT = 8099
INGRESS_PROXY = "172.30.32.2"
PAGE = os.path.join(os.path.dirname(__file__), "ui.html")


def pattern_json(p, names: dict) -> dict:
    return {
        "id": pattern_id(p),
        "entity_id": p.entity_id,
        "name": names.get(p.entity_id) or p.entity_id,
        "action": p.key.split(" → ", 1)[1],
        "when": p.when(),
        "anchor": p.anchor,
        "days": p.days,
        "hits": p.hits,
        "chances": p.chances,
        "unit": p.unit,
        "rate": round(p.rate, 3),
        "confidence": round(p.confidence, 3),
        "changed_at": p.changed_at,
        "spread": round(p.spread, 1),
        "other_spread": None if p.other_spread is None else round(p.other_spread, 1),
        "status": analysis.status(p),
        "progress": {k: round(v, 3) for k, v in analysis.progress(p).items()},
    }


def _numeric(state) -> bool:
    try:
        float(state)
        return True
    except (TypeError, ValueError):
        return False


def build_app(learner, allow_any: bool = False) -> web.Application:
    @web.middleware
    async def only_ingress(request, handler):
        if not allow_any and request.remote != INGRESS_PROXY:
            raise web.HTTPForbidden(text="Nur über Home Assistant erreichbar")
        return await handler(request)

    def names() -> dict:
        return {e: s.get("attributes", {}).get("friendly_name")
                for e, s in learner.states.items()}

    def automations() -> list[dict]:
        out = []
        for s in learner.store.automated():
            runs = learner.reviewer.runs(s)
            out.append({**s, "balance": review.balance(runs),
                        "delete_at": s["paused"] + review.DELETE_DAYS * 86400
                        if s["paused"] else None})
        return out

    def overview() -> dict:
        n = names()
        shadow = {pid: (hits, total)
                  for pid, hits, total in learner.store.prediction_stats(28)}
        pilot = learner.store.autopilot()
        counts = learner.store.autopilot_counts()
        patterns = []
        for p in learner.patterns:
            item = pattern_json(p, n)
            hits, total = shadow.get(item["id"], (0, 0))
            item["shadow"] = {"hits": hits, "total": total}
            status = pilot.get(item["id"], (None,))[0]
            runs, undone = counts.get(item["id"], (0, 0))
            item["autopilot"] = {"status": status, "runs": runs, "undone": undone or 0}
            patterns.append(item)
        return {
            "analysed_at": learner.analysed_at,
            "now": time.time(),
            "connected": learner.ws is not None,
            "stats": learner.store.stats(),
            "thresholds": {"confidence": analysis.CONFIDENCE, "rate": analysis.MIN_RATE,
                           "spread": analysis.MAX_SPREAD,
                           "window_days": analysis.WINDOW_DAYS},
            "patterns": patterns,
            "suggestions": learner.store.suggestions(),
            "automations": automations(),
            "discovery": {"phones": len(learner.suggester.notify),
                          "speakers": len(learner.suggester.players()),
                          "announce": learner.suggester.announce,
                          "autopilot": learner.autopilot.enabled},
        }

    def presence() -> dict:
        candidates = sorted(
            ({"entity_id": e, "name": s.get("attributes", {}).get("friendly_name") or e,
              "state": s.get("state")}
             for e, s in learner.states.items()
             if e.split(".", 1)[0] in analysis.PRESENCE_DOMAINS
             and (not e.startswith("sensor.") or _numeric(s.get("state")))),
            key=lambda c: (c["entity_id"].split(".")[0], c["name"].lower()))
        return {"selected": list(learner.presence_entities), "candidates": candidates}

    async def api_presence(_):
        return web.json_response(presence())

    async def api_set_presence(request):
        body = await request.json()
        chosen = body.get("entities")
        if not isinstance(chosen, list) or not all(
                isinstance(e, str) and e in learner.states for e in chosen):
            raise web.HTTPBadRequest(text="Unbekannte Entität")
        learner.set_presence(chosen)
        return web.json_response(presence())

    async def page(_):
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-store"})

    async def api_overview(_):
        return web.json_response(overview())

    async def api_analyse(_):
        learner.run_analysis()
        return web.json_response(overview())

    app = web.Application(middlewares=[only_ingress])
    app.add_routes([web.get("/", page),
                    web.get("/api/overview", api_overview),
                    web.post("/api/analyse", api_analyse),
                    web.get("/api/presence", api_presence),
                    web.post("/api/presence", api_set_presence)])
    return app


async def start(learner):
    runner = web.AppRunner(build_app(learner))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Oberfläche bereit (Ingress, Port %d)", PORT)
