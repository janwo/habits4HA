# Entscheidungen

Getroffene Entscheidungen mit Begründung – damit spätere Arbeit (auch durch
andere Personen oder KI-Assistenten) darauf aufbaut, statt sie neu zu
diskutieren. Neue Entscheidungen unten anhängen; überholte nicht löschen,
sondern als **ersetzt durch Dxx** markieren.

Format: **Dxx – Titel** · Datum · Status
Kontext → Entscheidung → Folgen.

---

## D01 – Öffentliches Repo: nichts Persönliches committen
2026-10-29 · gilt

**Kontext:** Das Repo ist öffentlich. Sicherheit hat oberste Priorität.

**Entscheidung:** Vor jedem Commit den Diff prüfen auf Namen, E-Mails,
Tokens, Passwörter, IPs, Hostnamen, lokale Pfade, HA-Benutzer-IDs und
Entitäts-IDs aus einer echten Installation. In Tests und Doku nur neutrale
Platzhalter (`person.alex`, `light.wohnzimmer`). Commits mit der
GitHub-noreply-Adresse.

**Folgen:** Persönliche Einstellungen gehören in die Add-on-Optionen oder
werden aus Home Assistant erkannt (→ D07). Die Historie wurde am 2026-10-29
einmal bereinigt und per Force-Push ersetzt.

## D02 – Add-on mit Supervisor-WebSocket, s6 mit `with-contenv`
2026-10-29 · gilt

**Kontext:** Ohne `with-contenv` kam `SUPERVISOR_TOKEN` nicht im Skript an →
`auth_invalid`.

**Entscheidung:** `run.sh` startet mit `#!/usr/bin/with-contenv sh`;
Verbindung über `ws://supervisor/core/websocket`, REST über
`http://supervisor/core/api`.

## D03 – Rohdaten in SQLite, Analyse im Add-on, keine externen Dienste
2026-10-29 · gilt

**Entscheidung:** Alle Ereignisse in `/data/habits.db` (Teil der
HA-Backups); Auswertung in reinem Python ohne zusätzliche Bibliotheken;
nichts verlässt die Installation.

## D04 – Aktionen, Kontext und Auslöser getrennt erfassen
2026-10-29 · gilt

**Entscheidung:**
- *Aktionen* (beobachtete `domains`) mit Herkunft `manual` / `automation` /
  `device` / `system` / `self`, Bereich, auslösender Automation und
  Kontext-Momentaufnahme (Sonnenhöhe, wer zuhause, aktive Mediaplayer) –
  Stand *vor* der Aktion.
- *Auslöser* (`person.*` immer, dazu `trigger_entities`) als eigene
  Ereignisse; Ausgangszustand bei jedem Verbinden, damit der Verlauf
  lückenlos ist.
- Nur `manual` und `device` fließen in die Mustererkennung. `self` (eigene
  Ansagen, Autopilot) wird nie mitgelernt.

## D05 – Auswertung: Gelegenheiten zählen, Streuung entscheidet
2026-10-29 · gilt

**Kontext:** Korrelation ist nicht Auslöser (Beispiel: Licht aus 5 min nach
dem Wecker, nicht um 23:05). Bei Abwesenheit entstehen keine Ereignisse.

**Entscheidung:**
- Gezählt wird pro *Gelegenheit* (Tag mit Anwesenheit), nicht pro Ereignis;
  Tage ohne Anwesenheit zählen nicht als verpasst.
- Auslöser ist der Bezugspunkt mit der geringsten Streuung (Uhrzeit vs.
  Sonnenhöhe, später Ereignisse). Lassen sich Kandidaten nicht trennen:
  kein Vorschlag, sondern warten oder fragen.
- Schwellen: ≥ 5 Treffer, ≥ 60 % der Gelegenheiten, Streuung ≤ 20 min.
- Werktags/Wochenende nur, wenn die Trefferquote dadurch um ≥ 15 Punkte
  steigt; sonst täglich.
- Lieber wenige sichere Vorschläge als viele falsche.

## D06 – Schweigen ist mehrdeutig; automatisierte Muster werden weiter gemessen
2026-10-29 · gilt

**Entscheidung:** Ein umgesetztes Muster verschwindet aus den manuellen
Daten – das ist Erfolg. Ab dann zählt die Trefferquote der Automation
(gelassen / korrigiert / rückgängig). „Nichts passiert“ gilt nur als
Zustimmung, wenn jemand es bemerken konnte; es gibt nur Anwesenheit für die
ganze Wohnung, keine Bewegungsmelder.

## D07 – Keine persönlichen IDs in der Konfiguration: alles erkennen
2026-10-29 · gilt

**Entscheidung:** Handys (`notify.mobile_app_*`), Zuordnung Person → Handy
(über `device_trackers`), Lautsprecher (Integration `cast`) und Bereiche
werden aus Home Assistant erkannt. Optionen enthalten nur Verhalten
(`announce`, Ruhezeiten, …), keine Geräte.

## D08 – Schattenmodus vor jeder Automatik
2026-10-29 · gilt

**Entscheidung:** Bereite Muster werden zuerst live vorhergesagt, ohne zu
schalten, und mit dem tatsächlichen Verhalten verglichen
(treffer / abweichend / verpasst). Keine Vorhersage bei Abwesenheit oder
wenn der Zielzustand schon besteht.

## D09 – Vorschlag im Moment, direkt *nach* der eigenen Aktion
2026-10-29 · gilt

**Entscheidung:**
- Vorschlag nach einem Schattentreffer – nicht davor, damit eine falsche
  Vorhersage nichts anrichtet.
- Handy-Benachrichtigung mit Übernehmen / Nein danke / Später, an die
  Person, die gehandelt hat, sonst an alle Anwesenden. Cast-Lautsprecher
  können nicht zuhören → Ansage nur als Hinweis, standardmäßig aus, nie in
  der Ruhezeit oder bei laufenden Medien.
- Höchstens ein Vorschlag pro Tag; „Nein danke“ endgültig, „Später“ nach
  3 Tagen erneut.
- Übernehmen legt `habit_learner_<nr>` an, immer mit Bedingung „jemand
  zuhause“.

## D10 – Autopilot nur experimentell, opt-in, mit harten Grenzen
2026-10-29 · geplant

**Entscheidung:** Option `autopilot` (Standard aus). Freigabe je Muster erst
nach z. B. 10/10 im Schattenmodus. Jede Aktion sofort mit Rückgängig melden;
zweimal Rückgängig → zurück in den Schattenmodus. Nur Licht, Szenen, Medien
– nie Schlösser, Alarm, Heizung; Rollläden nur ausdrücklich.

## D11 – Tests mit erfundenen Daten statt echter Aufzeichnungen
2026-10-29 · gilt

**Entscheidung:** `habit_learner/tests/` erzeugt synthetische Wochen mit
eingebauten Mustern und Fallen (Wecker-Folgemuster, Rauschen, Urlaub,
bereits automatisiert). Echte Datenbanken oder Logs kommen nie ins Repo
(→ D01). Tests laufen ohne HA: `python3 habit_learner/tests/test_*.py`.

## D12 – Kein Force-Push auf `main`
2026-10-29 · gilt

**Kontext:** Home Assistant hält eine eigene Kopie des Repos. Nach dem
Umschreiben der Historie (D01) bot HA kein Update mehr an.

**Entscheidung:** `main` wird nie mehr umgeschrieben. Fehler werden mit neuen
Commits korrigiert. Muss doch einmal etwas aus der Historie entfernt werden:
vorher mit dem Nutzer klären und erklären, wie das Repo in HA neu eingebunden
wird.

## D13 – Oberfläche als Ingress-Seite im Add-on, ohne Build-Schritt
2026-10-29 · gilt

**Entscheidung:** Seitenleisten-Panel über HA-Ingress; aiohttp liefert eine
einzelne HTML-Seite (ohne Framework, ohne CDN) und eine JSON-API unter
`api/…`. Zugriff nur über den Ingress-Proxy des Supervisors. Zeigt
Vorschläge, fast erreichte Muster mit Fortschritt zu den Schwellen (D05),
Schattenbilanz und Vorschläge mit Antworten.

## D14 – Angelegte Automationen nachmessen, pausieren, löschen
2026-10-30 · gilt (setzt D06 um)

**Kontext:** Eine angenommene Automation kann sich als falsch herausstellen.
Sie soll nicht ewig bleiben.

**Entscheidung:**
- Jeder Lauf einer `habit_learner_*`-Automation wird bewertet: *rückgängig*
  (innerhalb 15 min von Hand umgeschaltet), *korrigiert* (gleicher Zustand,
  andere Helligkeit/Farbe), *gelassen* (nichts geändert, jemand zuhause).
  Läufe ohne Anwesenheit zählen nicht.
- Von den letzten 5 bewerteten Läufen ≥ 3× rückgängig → automatisch
  pausieren (in HA deaktivieren) und per Handy melden. *Korrigiert* pausiert
  nicht.
- Schaltet jemand sie wieder ein → gilt als angenommen, Messung beginnt neu.
- 14 Tage pausiert → gelöscht. Das Muster wird danach nicht erneut
  vorgeschlagen.
- Nur eigene Automationen (`habit_learner_*`) werden angefasst.
