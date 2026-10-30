# habits4HA

Home-Assistant-App, die wiederkehrende manuelle Aktionen erkennt und daraus
Automationen vorschlägt – z. B. „nach dem Wecker nach ~10 min Licht aus“ oder
„Tischlampe morgens warmweiß, abends rot“.

## Installation

1. Einstellungen → Apps → App Store → ⋮ → **Repositories**
2. `https://github.com/janwo/habits4HA` hinzufügen
3. **Habit Learner** installieren und starten

## Status

| Phase | Inhalt | Stand |
|---|---|---|
| 1 | Ereignisse erfassen (manuell / Automation / Gerät) in SQLite | ✅ v0.1.0 |
| 1b | Kontext, Bereiche, auslösende Automation, Auslöser-Ereignisse | ✅ v0.2.0 |
| 2 | Mustererkennung: Zeit- und Sonnenmuster (Ausgabe im Log) | ✅ v0.3.0 |
| 3 | Schattenmodus: Vorhersagen live prüfen, Bilanz im Log | ✅ v0.4.0 |
| 3b | Folgemuster (Wecker → Licht aus), Kontext → Einstellung (Licht je Tageszeit) | ✅ v0.8.0 |
| 4 | Vorschläge im Moment: Benachrichtigung, Ansage, Automation anlegen | ✅ v0.5.0 |
| 4b | Autopilot je Muster (experimentell, Option `autopilot`) | ✅ v0.8.0 |
| 5 | Seitenleiste: Muster, Fortschritt zu den Schwellen, Schattenbilanz, Vorschläge | ✅ v0.6.0 |
| 6 | Angelegte Automationen nachmessen; oft rückgängig → pausieren, nach 14 Tagen löschen | ✅ v0.7.0 |

## Herkunft von Ereignissen

| Herkunft | Bedeutung |
|---|---|
| `manual` | Mensch über HA: App, Dashboard, Google Assistant, Assist |
| `automation` | Folge einer Automation oder eines Skripts |
| `device` | direkt am Gerät, z. B. Wandschalter |
| `system` | Verfügbarkeit, Neustarts |

Zu jeder Aktion speichert die App außerdem den Bereich, die auslösende
Automation und eine Momentaufnahme des Kontexts (Sonnenhöhe, wer zuhause ist,
aktive Mediaplayer). Details: [docs/konzept.md](docs/konzept.md).

Handys (`notify.mobile_app_*`), Lautsprecher (Cast) und Personen erkennt die
App selbst. Ein Vorschlag geht an das Handy der Person, die gehandelt hat –
sonst an alle, die zuhause sind. „Übernehmen“ legt eine Automation
`habit_learner_<nr>` an.

Nur `manual` und `device` fließen später in die Mustererkennung ein.

## Optionen

| Option | Standard | Beschreibung |
|---|---|---|
| `retention_days` | 180 | wie lange Ereignisse gespeichert bleiben |
| `domains` | light, switch, scene, … | welche Entitätstypen beobachtet werden |
| `exclude_users` | – | HA-Benutzer-IDs, die ignoriert werden (z. B. Gäste) |
| `announce` | aus | kurze Ansage über einen Cast-Lautsprecher beim Vorschlag |
| `quiet_start` / `quiet_end` | 22:00 / 07:00 | Ruhezeit ohne Ansagen |
| `autopilot` | aus | experimentell: nach 10 von 10 Treffern im Schattenmodus selbst schalten, jedes Mal mit „Rückgängig“ (nur Licht, Szenen, Medien) |
| `presence_entities` | – (erkannt) | Anwesenheit der Wohnung als Entität, z. B. ein `binary_sensor`, `input_boolean`, `group` oder `zone.home`. Leer: eine Entität namens `presence`, `anwesenheit`, `anwesend` oder `zuhause` wird erkannt, sonst gelten Personen (`person.*`) |
| `trigger_entities` | – | zusätzliche mögliche Auslöser, z. B. `sensor.<handy>_next_alarm`; Personen (`person.*`) werden immer erfasst |

Die Daten liegen in `/data/habits.db` innerhalb der App und sind Teil
der HA-Backups.
