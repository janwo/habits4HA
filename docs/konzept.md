# Konzept: Habit Learner

Ziel: wiederkehrende Handlungen erkennen und als Automationen vorschlagen –
lieber wenige, sichere Vorschläge als viele kluge, aber falsche.

## 1. Was erkannt werden soll

| Mustertyp | Beispiel |
|---|---|
| Zeitmuster | Kaffeemaschine werktags ~6:50 an |
| Sonnenmuster | Wohnzimmerlicht an, wenn Sonnenhöhe < −2° |
| Folgemuster (A → B) | Wecker gestellt → 5 min später Schlafzimmerlicht aus |
| Szenen | diese 4 Lampen werden fast immer zusammen geschaltet |
| Kontext → Einstellung | Tischlampe morgens warmweiß, abends rot |
| Anwesenheit | Heimkommen nach Sonnenuntergang → Flurlicht an |
| Dauer | Licht immer ~2 h nach dem Einschalten aus |
| Korrekturen | Automation schaltet an, du dimmst fast immer sofort auf 40 % |
| Fernbedienung | unterwegs per App Heizung an → „beim Losfahren“ |

## 2. Daten

### Aktionen vs. Kontext

- **Aktionen** – was vorhergesagt werden soll: Änderungen an den beobachteten
  `domains` (Licht, Schalter, …), mit Herkunft `manual` / `automation` /
  `device` / `system`.
- **Kontext** – was Aktionen erklärt. Wird bei jeder Aktion als
  Momentaufnahme mitgespeichert:
  - Sonnenhöhe und -richtung (`sun.sun`: `elevation`, `rising`)
  - Anwesenheit (`person.*`)
  - Medien-Zustände
  - Bereich (`area_id`) der Entität
- **Auslöser-Kandidaten** – Entitäten, deren Änderungen selbst Auslöser sein
  können, werden zusätzlich als eigene Ereignisse gespeichert (Option
  `trigger_entities`): Wecker (`sensor.<handy>_next_alarm`), Anwesenheit,
  Medien. Was HA nicht sieht, kann nie Auslöser sein.

### Pflichtangaben je Ereignis

- auslösende Automation (über `context.parent_id` → `automation.*`)
- Bereich der Entität
- Kontext-Momentaufnahme

### Anwesenheit lückenlos

Kommen/Gehen als Ereignisse, plus Ausgangszustand beim Start des Add-ons.
Nur so lässt sich für jeden Zeitpunkt rekonstruieren, wer da war.

## 3. Auswertung

### Vorbereitung

- Ereignisse zusammenfassen: mehrere Änderungen in ~10 s = eine Aktion (Endwert).
- Werte vergröbern: Helligkeit in Stufen, Farbe als Kategorie
  (warm/neutral/kalt/farbig), Zeit in 15-min-Fenstern.
- Tage mit > 24 h Abwesenheit (Urlaub) aus der Statistik nehmen.

### Gelegenheiten statt Ereignisse

Gezählt wird pro **Gelegenheit** (jeder Tag bei Zeitmustern, jedes Auftreten
von A bei Folgemustern), nicht nur, wenn etwas passiert. Sonst kann nie
gelernt werden, wann ein Muster *nicht* gilt – etwa bei Abwesenheit.

### Auslöser finden: Streuung entscheidet

Für eine wiederkehrende Aktion werden alle Kandidaten davor betrachtet
(Uhrzeit, Sonnenhöhe, vorausgehende Ereignisse). Auslöser ist der
Bezugspunkt, an dem die Aktion am festesten hängt:

| Abend | Wecker | Licht aus | rel. zu 23:05 | rel. zum Wecker |
|---|---|---|---|---|
| Mo | 22:40 | 22:45 | −20 min | +5 min |
| Di | 23:10 | 23:15 | +10 min | +5 min |
| Mi | 00:20 | 00:24 | +79 min | +4 min |

Zusätzlich Gegenprobe in beide Richtungen:
- Folgt auf A meistens B? (Konfidenz)
- Geht B meistens A voraus? (Abdeckung)

Lassen sich Kandidaten nicht trennen (fallen immer zusammen): kein Vorschlag,
sondern warten oder fragen. Im Zweifel ist ein Ereignis als Auslöser die
vorsichtigere Wahl als eine feste Uhrzeit.

### Wahrnehmung: Schweigen ist mehrdeutig

„Nichts passiert“ nach einer Automation heißt nur dann Zustimmung, wenn
jemand es bemerken konnte. Ohne Bewegungsmelder (nur Anwesenheit für die
Wohnung) gilt:

- niemand zuhause → **keine Aussage**
- keine Bedienung irgendwo seit langem, nachts → vermutlich Schlaf, keine Aussage
- Bedienung im selben Bereich danach → war wahrnehmbar
- Änderung kurz nach dem Heimkommen → Korrektur

Eine spät und zufällig ausgeschaltete vergessene Lampe streut breit und
erzeugt dadurch von selbst kein Muster.

### Automatisierte Muster

Ein umgesetztes Muster verschwindet aus den manuellen Daten – das ist
Erfolg, kein Datenverlust. Ab dann wird die **Automation** gemessen:

- unverändert gelassen (bei Wahrnehmbarkeit) → Zustimmung
- Wert geändert → Vorschlag: anderen Wert setzen
- rückgängig gemacht → Vorschlag: Bedingung ergänzen

Ergibt eine Trefferquote je Automation. Zwei Sichten: manuell (neue
Gewohnheiten) und gesamt (verhindert doppelte Vorschläge).

### Anwesenheit als Bedingung

- wird über die Gelegenheiten-Zählung automatisch gelernt
- **Leitplanke:** jeder Vorschlag bekommt standardmäßig „nur wenn jemand
  zuhause“, außer das Muster ist ausdrücklich auch bei Abwesenheit belegt
- manuelle Aktionen bei Abwesenheit (aus der Ferne) bleiben erhalten

### Bewertung eines Vorschlags

Häufigkeit × Zuverlässigkeit × Ersparnis. Kein Vorschlag, wenn eine
Automation das schon abdeckt oder er abgelehnt wurde.

## 4. UX

### Grundsätze

- **Ruhig:** höchstens ein Vorschlag pro Tag, nie nachts, nie bei Abwesenheit.
  Lieber gebündelt als dauernd.
- **Erklärbar:** jeder Vorschlag sagt *was*, *wann* und *warum* – mit Zahlen.
  > „Wenn du nach 21 Uhr den Wecker stellst, Schlafzimmerlicht nach 5 min
  > aus. Passte an 14 von 15 Abenden – an der Uhrzeit hängt es nicht.“
- **Ein Tipp genügt:** Übernehmen legt die Automation direkt an, ohne YAML.
- **Rückgängig jederzeit:** übernommene Vorschläge sind als solche markiert
  und mit einem Tipp wieder entfernt.
- **Fragen statt raten:** Grenzfälle werden zu kurzen Ja/Nein-Fragen
  („Absicht oder vergessen?“); Antworten fließen in die Auswertung.
- **Klartext:** keine Begriffe wie Konfidenz oder Lift in der Oberfläche.

### Benachrichtigung (Companion-App)

Aktionen: **Übernehmen** · **Anpassen** · **Nein danke** · **Später**.
„Anpassen“ öffnet den Vorschlag in der Seitenleiste.
Google-Home-Ansage nur als Option, standardmäßig aus – gesprochen eignen
sich nur kurze Hinweise („Ich habe einen Vorschlag fürs Schlafzimmerlicht“).

### Seitenleiste (Ingress)

- **Vorschläge:** Karten mit Erklärung, kleiner Zeitleiste der Belege,
  Knöpfen wie oben; einstellbar: Auslöser, Wert, Bedingungen.
- **In Beobachtung:** Muster, die noch nicht sicher genug sind, mit
  Fortschritt („noch ~5 Abende“). Macht sichtbar, dass die App lernt.
- **Meine Automationen:** Trefferquote je Automation
  („41× ausgelöst, 39× so gelassen“) und Verbesserungsvorschläge.
- **Daten:** was beobachtet wird, wie viele Ereignisse, Ausschlüsse,
  Löschen.

### Vorschlag im Moment

Ein bereites Muster wird vorgeschlagen, **direkt nachdem** du die Aktion
selbst ausgeführt hast – nicht davor. Dann ist der Bezug klar und eine
falsche Vorhersage kann nichts anrichten.

- Benachrichtigung mit Knöpfen; zusätzlich kurze Ansage über den
  Lautsprecher im selben Bereich („Ich hätte einen Vorschlag fürs
  Wohnzimmerlicht – schau aufs Handy“). Cast-Lautsprecher können nicht
  zuhören, die Antwort kommt übers Handy.
- Keine Ansage nachts, kurz vor dem Schlafen oder wenn Medien laufen;
  höchstens eine pro Tag, jeder Vorschlag nur einmal.

### Schattenmodus und Autopilot (experimentell)

1. **Schattenmodus** (immer an, unsichtbar): Die App sagt Aktionen live
   voraus, führt sie aber nicht aus, und vergleicht mit dem, was du tust.
   Ergibt eine gemessene Trefferquote je Muster, bevor irgendetwas
   automatisch passiert.
2. **Autopilot je Muster:** nach z. B. 10 von 10 Treffern im Schattenmodus
   Frage „Soll ich das ab jetzt selbst machen?“. Danach handelt die App
   selbst und meldet es sofort mit **Rückgängig**-Knopf. Rückgängig senkt
   das Vertrauen stark; zweimal hintereinander → zurück in den Schattenmodus.
3. **Echte Automation:** läuft ein Muster wochenlang ohne Korrektur, Angebot,
   daraus eine normale HA-Automation zu machen.

Grenzen: Option `autopilot` (Standard aus); nur Licht, Szenen, Medien –
nie Schlösser, Alarm, Heizung, Rollläden nur ausdrücklich; nie bei
Abwesenheit ohne Beleg. Eigene Aktionen (Autopilot, Ansagen) werden
markiert und nicht mitgelernt.

### Einstieg

Nach der Installation ehrlich sagen, dass zunächst gesammelt wird:
„Ich beobachte jetzt. Erste Vorschläge in etwa 1–2 Wochen.“ Beim Einrichten
die Auslöser-Kandidaten (Wecker, Personen) anbieten statt sie suchen zu lassen.

## 5. Reihenfolge

1. ✅ Datensammlung erweitern: auslösende Automation, Bereich, Kontext,
   `trigger_entities`, Anwesenheit lückenlos
2. ✅ Analyse: Zeit- und Sonnenmuster, Ausgabe im Log
3. ✅ Schattenmodus: live vorhersagen und vergleichen
4. ✅ Vorschlag im Moment (Benachrichtigung, Ansage)
5. Autopilot je Muster
6. Folgemuster, Korrekturen, Kontext → Einstellung – gemessen über den
   Schattenmodus
7. Seitenleiste
