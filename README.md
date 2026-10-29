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
| 2 | Mustererkennung: Sequenzen und Kontext → Einstellung | geplant |
| 3 | Vorschläge per Google-Home-Ansage und App-Benachrichtigung | geplant |
| 4 | Weboberfläche in der Seitenleiste | geplant |

## Herkunft von Ereignissen

| Herkunft | Bedeutung |
|---|---|
| `manual` | Mensch über HA: App, Dashboard, Google Assistant, Assist |
| `automation` | Folge einer Automation oder eines Skripts |
| `device` | direkt am Gerät, z. B. Wandschalter |
| `system` | Verfügbarkeit, Neustarts |

Nur `manual` und `device` fließen später in die Mustererkennung ein.

## Optionen

| Option | Standard | Beschreibung |
|---|---|---|
| `retention_days` | 180 | wie lange Ereignisse gespeichert bleiben |
| `domains` | light, switch, scene, … | welche Entitätstypen beobachtet werden |
| `exclude_users` | – | HA-Benutzer-IDs, die ignoriert werden (z. B. Gäste) |

Die Daten liegen in `/data/habits.db` innerhalb der App und sind Teil
der HA-Backups.
