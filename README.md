# TimeTree Calendar für Home Assistant (Fork)

Ein vollständiger Fork von
[acdcnow/Timetree-Import-for-Home-Assistant](https://github.com/acdcnow/Timetree-Import-for-Home-Assistant)
v1.1.3, der alle 5 zum Zeitpunkt des Forks offenen GitHub-Issues behebt.

## Gefixte Issues

### #2 – `RRULE did not contain FREQ` (kritisch)

TimeTree liefert Wiederholungsregeln als rohe ICS-Content-Zeile inkl.
`RRULE:`-Prefix (z. B. `"RRULE:FREQ=YEARLY"`). Das Original reicht das
unverändert an Home Assistants `CalendarEvent` weiter, das nur den reinen
Wert (`FREQ=YEARLY`, ohne Prefix) erwartet – das bricht `get_events` für
jeden Abfragezeitraum, der ein wiederkehrendes Event enthält.

**Fix:** `ics_builder.py` baut bei jedem Poll ein vollständiges
In-Memory-`icalendar.Calendar` mit korrekt geparsten `RRULE`/`EXDATE`/
`RDATE`-Properties. `coordinator.py` expandiert wiederkehrende Events
anschließend selbst mit
[`recurring-ical-events`](https://pypi.org/project/recurring-ical-events/)
zu konkreten Einzelterminen (14 Tage rückwirkend, 400 Tage voraus). Home
Assistant bekommt dadurch nie eine rohe Wiederholungsregel zu Gesicht.

### #1 – "No longer appears functional"

Vom Reporter selbst als Folgeproblem von #2 identifiziert: Tagesansicht
funktionierte, Monats-/7-Tage-Ansicht (die zwangsläufig einen breiteren
Datumsbereich abfragt) nicht. Durch den #2-Fix automatisch behoben.

### #3 – `create_event` crasht mit `combine() argument 1 must be datetime.date, not None`

Der Originalcode liest `kwargs.get("start_date_time")` /
`kwargs.get("start_date")`. Home Assistant Core normalisiert
`create_event`-Daten aber zu `dtstart`/`dtend` (datetime für Termine mit
Uhrzeit, date für ganztägige) – diese Schlüssel existieren im Original
nicht, `start_dt` bleibt `None`.

**Fix:** `calendar.py` liest `dtstart`/`dtend`, erkennt Ganztägig via
`isinstance(start, datetime)`.

### #4 – Labels/Farben werden nicht durchgereicht

Feature-Request: TimeTree-Labels (mit denen z. B. "gemeinsamer Termin" vs.
"nur Person X" farblich unterschieden wird) kommen nicht in HA an.

**Fix:** `api.py` lädt Label-Definitionen über
`GET /calendar/{id}/labels`. Die Label-ID überlebt die
Wiederholungs-Expansion (als `X-TIMETREE-LABEL-ID`-Property). Name und
Farbe des nächsten Termins sowie alle bekannten Labels stehen als
`extra_state_attributes` der Entity zur Verfügung (Developer Tools >
States, `state_attr()` in Templates) – nutzbar für eigene
Automationen/Dashboard-Karten. **Hinweis:** Weder die native iOS-Kalender-
App noch gängige Lovelace-Kalender-Karten (inkl. Calendar Card Pro)
rendern pro-Termin-Farben – das bleibt unabhängig vom hier verfügbaren
Rohdatenzugriff so.

### #5 – `create_event` end-to-end kaputt (4 verschachtelte Defekte)

Ausführlichster Report, inkl. per Packet-Capture des TimeTree-Web-Clients
root-gecausten Patch. Vier unabhängige Defekte:

1. **Falsche kwargs** (identisch mit #3)
2. **HTTP 422** – falscher Endpoint (`/events` statt `/event`, Singular),
   unvollständiger Payload (fehlend: `label_id`, `attendees`,
   `attachment.virtual_user_attendees`, `recurrences: []`, `alerts: []`)
3. **TLS-Fingerprinting** – der Endpoint prüft zusätzlich zu HTTP-Headern
   den TLS-ClientHello (JA3/JA4); normale `requests`/`curl`-Anfragen werden
   unabhängig von Headern abgelehnt. Erfordert außerdem ein frisches
   CSRF-Token (gescraped von einer Kalenderseite) sowie
   `Sec-Fetch-*`/`Origin`/`Referer`-Header.
4. **Off-by-one bei Ganztägig-Terminen**, symmetrisch bei Schreiben und
   Lesen: HA liefert ein *exklusives* Enddatum (RFC5545), TimeTree erwartet
   ein *inklusives* (`start_at == end_at` bei 1-Tages-Events). Zusätzlich
   müssen Ganztägig-Zeitstempel exakt auf UTC-Mitternacht liegen, nicht auf
   lokale Zeitzone.

**Fix:** `api.py` nutzt [`curl_cffi`](https://pypi.org/project/curl-cffi/)
mit `impersonate="firefox135"` für einen Browser-typischen
TLS-Fingerprint, den korrekten Singular-Endpoint mit vollständigem
Payload, CSRF-Token-Scraping vor jedem Schreibzugriff, sowie die
Exklusiv-/Inklusiv-Umrechnung symmetrisch auf Schreib- und Leseseite.

## Bekannte Einschränkungen / offene Punkte

- **Inoffizielle API.** Wie das Original nutzt dieser Fork TimeTrees
  internes Web-Client-API. Ändert TimeTree Backend oder Anti-Bot-Maßnahmen,
  kann die Integration jederzeit ohne Vorwarnung brechen – insbesondere der
  Schreibpfad (Punkt 3 oben) ist von Natur aus fragil.
- **`label_id` beim Erstellen:** Es wird automatisch das erste verfügbare
  Label des Kalenders verwendet (TimeTree verlangt zwingend eines pro
  Event). Für gezielte Label-Auswahl beim Erstellen müsste die
  `create_event`-Signatur um einen Parameter erweitert werden – aktuell
  nicht über den Options-Flow konfigurierbar.
- **`impersonate="firefox135"`** (Issue #5, "Caveats"): Muss ggf. mit
  künftigen `curl_cffi`-Releases synchron gehalten werden, falls TimeTrees
  Anti-Bot-Erkennung das Profil irgendwann ablehnt.
- Die Schreibfunktion wurde anhand der sehr detaillierten Issue-#5-Analyse
  implementiert und lokal (ohne echten TimeTree-Zugriff, da das
  Sandbox-Netzwerk keinen Zugriff auf `timetreeapp.com` hatte) auf
  Payload-Korrektheit und Off-by-one-Symmetrie getestet – ein Live-Test
  gegen einen echten TimeTree-Account steht noch aus.

## Installation

### Via HACS (Custom Repository)

1. HACS → Integrationen → drei Punkte oben rechts → **Custom repositories**
2. Repository-URL eintragen, Kategorie **Integration**
3. "TimeTree Calendar" installieren, HA neu starten

### Manuell

`custom_components/timetree` nach `/config/custom_components/` kopieren,
HA neu starten.

## Einrichtung

Einstellungen → Geräte & Dienste → Integration hinzufügen → "TimeTree
Calendar" → E-Mail/Passwort → Kalender + Abfrageintervall wählen.
