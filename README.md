# TimeTree Calendar for Home Assistant (Fork)

A complete fork of
[acdcnow/Timetree-Import-for-Home-Assistant](https://github.com/acdcnow/Timetree-Import-for-Home-Assistant)
v1.1.3, fixing all 5 issues that were open on the original repository at
the time of forking.

## Issues fixed

### #2 – `RRULE did not contain FREQ` (critical)

TimeTree returns recurrence rules as a raw ICS content line including the
`RRULE:` prefix (e.g. `"RRULE:FREQ=YEARLY"`). The original passes this
straight through to Home Assistant's `CalendarEvent`, which expects only
the bare value (`FREQ=YEARLY`, no prefix) — this breaks `get_events` for
any date range that includes a recurring event.

**Fix:** `ics_builder.py` builds a full in-memory `icalendar.Calendar` on
every poll, with correctly parsed `RRULE`/`EXDATE`/`RDATE` properties.
`coordinator.py` then expands recurring events itself using
[`recurring-ical-events`](https://pypi.org/project/recurring-ical-events/)
into concrete single occurrences (14 days back, 400 days ahead). Home
Assistant never sees a raw recurrence rule at all.

### #1 – "No longer appears functional"

Identified by the reporter themself as a downstream effect of #2: day view
worked, month/7-day view (which necessarily queries a wider date range)
did not. Resolved automatically by the #2 fix.

### #3 – `create_event` crashes with `combine() argument 1 must be datetime.date, not None`

The original code reads `kwargs.get("start_date_time")` /
`kwargs.get("start_date")`. Home Assistant Core actually normalises
`create_event` data into `dtstart`/`dtend` (datetime for timed events,
date for all-day events) — those keys never existed in the original, so
`start_dt` was always `None`.

**Fix:** `calendar.py` reads `dtstart`/`dtend`, detecting all-day via
`isinstance(start, datetime)`.

### #4 – Labels/colors are not passed through

Feature request: TimeTree labels (used e.g. to distinguish "shared event"
from "just person X") never reach Home Assistant.

**Fix:** `api.py` fetches label definitions via
`GET /calendar/{id}/labels`. The label id survives recurrence expansion
(carried as an `X-TIMETREE-LABEL-ID` property). The next event's label
name/color, plus all known labels, are exposed via the entity's
`extra_state_attributes` (visible in Developer Tools > States, usable via
`state_attr()` in templates) — usable for custom automations/dashboard
cards. **Note:** neither the native iOS calendar app nor common Lovelace
calendar cards (including Calendar Card Pro) render per-event colors —
that remains true regardless of this raw-data access being available.

### #5 – `create_event` broken end-to-end (4 stacked defects)

The most thorough report, including a patch root-caused via packet
capture of the TimeTree web client. Four independent defects:

1. **Wrong kwargs** (same as #3)
2. **HTTP 422** — wrong endpoint (`/events` instead of the singular
   `/event`), incomplete payload (missing `label_id`, `attendees`,
   `attachment.virtual_user_attendees`, `recurrences: []`, `alerts: []`)
3. **TLS fingerprinting** — the endpoint checks the TLS ClientHello
   (JA3/JA4) in addition to HTTP headers; plain `requests`/`curl` requests
   are rejected regardless of headers. Also requires a fresh CSRF token
   (scraped from a calendar page) plus `Sec-Fetch-*`/`Origin`/`Referer`
   headers.
4. **Off-by-one on all-day events**, symmetric on write and read: HA
   supplies an *exclusive* end date (RFC5545 convention), TimeTree expects
   an *inclusive* one (`start_at == end_at` for a 1-day event).
   All-day timestamps must also land exactly on UTC midnight, not the
   local timezone.

**Fix:** `api.py` uses [`curl_cffi`](https://pypi.org/project/curl-cffi/)
with `impersonate="firefox135"` for a browser-shaped TLS fingerprint, the
correct singular endpoint with a complete payload, CSRF token scraping
before every write, and the exclusive/inclusive conversion symmetrically
on both the write and read side.

## Known limitations / open items

- **Unofficial API.** Like the original, this fork relies on TimeTree's
  internal web-client API. If TimeTree changes their backend or anti-bot
  measures, the integration can break without notice at any time —
  especially the write path (point 3 above), which is inherently fragile.
- **`label_id` on create:** the first available label on the calendar is
  used automatically (TimeTree requires every event to have one). Picking
  a specific label when creating an event would require extending the
  `create_event` signature — not currently configurable via the options
  flow.
- **`impersonate="firefox135"`** (issue #5, "Caveats"): may need to be
  kept in sync with future `curl_cffi` releases if TimeTree's anti-bot
  detection ever rejects this profile.
- The write functionality was implemented based on the very detailed
  issue #5 analysis and tested locally (without real TimeTree access,
  since the sandbox network had no access to `timetreeapp.com`) for
  payload correctness and off-by-one symmetry — a live test against a
  real TimeTree account is still pending.

## Installation

### Via HACS (Custom Repository)

1. HACS → Integrations → three dots top right → **Custom repositories**
2. Enter the repository URL, category **Integration**
3. Install "TimeTree Calendar", restart HA

### Manual

Copy `custom_components/timetree` to `/config/custom_components/`,
restart HA.

## Setup

Settings → Devices & Services → Add Integration → "TimeTree Calendar" →
email/password → pick calendar + poll interval.
