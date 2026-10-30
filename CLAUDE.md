# habits4HA

Home-Assistant-Add-on, das Gewohnheiten erkennt und als Automationen vorschlägt.

**Vor jeder Arbeit lesen:**
- [docs/decisions.md](docs/decisions.md) – getroffene Entscheidungen; nicht neu
  diskutieren, neue Entscheidungen dort anhängen
- [docs/konzept.md](docs/konzept.md) – Konzept und Reihenfolge der Schritte

**Öffentliches Repo (D01):** Vor jedem Commit den Diff auf Persönliches
prüfen – Namen, E-Mails, Tokens, IPs, lokale Pfade, Entitäts- und
Benutzer-IDs aus echten Installationen. Nur neutrale Platzhalter.

**Tests:** `python3 habit_learner/tests/test_analysis.py`,
`test_shadow.py`, `test_suggest.py`, `test_review.py`, `test_autopilot.py` – laufen ohne Home Assistant.

**Release:** Bei jeder Änderung am Add-on `version` in
`habit_learner/config.yaml` erhöhen, sonst bietet HA kein Update an.
