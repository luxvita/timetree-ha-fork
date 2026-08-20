"""API Client for TimeTree.

Authentication and read (event/calendar/label) endpoints use TimeTree's
internal web-client API, the same approach as `timetree-exporter`
(eoleedi/TimeTree-Exporter) and the original acdcnow integration.

The write path (`create_event`) additionally implements the fixes
root-caused in GitHub issue #5 of the original repository
(acdcnow/Timetree-Import-for-Home-Assistant), specifically:

  - Defect 2: correct endpoint (`/calendar/{id}/event`, singular) and a
    complete payload (label_id, attendees, nested attachment, recurrences,
    alerts).
  - Defect 3: TimeTree's create-event endpoint sits behind a WAF/anti-bot
    layer that checks the TLS ClientHello fingerprint (JA3/JA4) in addition
    to normal HTTP headers, plus a CSRF token scraped from a calendar page
    and browser-shaped Sec-Fetch-*/Origin/Referer headers. We use
    `curl_cffi` with `impersonate="firefox135"` to produce a browser-shaped
    TLS handshake, since plain `requests`/`curl` are rejected regardless of
    headers.
  - Defect 4: all-day dates are off by one day, symmetrically on write and
    read (see `_all_day_to_timestamp` / `parse_event` below).

This remains an unofficial, reverse-engineered API and may break without
notice if TimeTree changes their backend or anti-bot measures.
"""
import logging
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from curl_cffi import requests as curl_requests

from homeassistant.core import HomeAssistant

from .const import CURL_CFFI_IMPERSONATE

_LOGGER = logging.getLogger(__name__)

API_BASEURI = "https://timetreeapp.com/api/v1"
WEB_BASEURI = "https://timetreeapp.com"
API_USER_AGENT = "web/2.1.0/en"

_CSRF_META_RE = re.compile(
    r'<meta\s+name="csrf-token"\s+content="([^"]+)"', re.IGNORECASE
)


class TimeTreeAuthError(Exception):
    """Raised when login fails."""


class TimeTreeApiError(Exception):
    """Raised for non-auth API failures (e.g. create_event rejected)."""


class TimeTreeApi:
    """TimeTree API Client."""

    def __init__(self, hass: HomeAssistant, email: str, password: str):
        self._hass = hass
        self._email = email
        self._password = password
        self._session_id = None
        self._user_id = None
        # A single curl_cffi session is used for everything so that cookies
        # picked up during login are automatically reused for the
        # TLS-fingerprint-sensitive create_event call later.
        self._session = curl_requests.Session(impersonate=CURL_CFFI_IMPERSONATE)

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    def _login(self):
        """Log in to TimeTree, get a session ID, and resolve the user id."""
        url = f"{API_BASEURI}/auth/email/signin"
        payload = {
            "uid": self._email,
            "password": self._password,
            "uuid": str(uuid.uuid4()).replace("-", ""),
        }
        headers = {
            "Content-Type": "application/json",
            "X-Timetreea": API_USER_AGENT,
        }

        _LOGGER.debug("Attempting login for user: %s", self._email)

        try:
            response = self._session.put(url, json=payload, headers=headers, timeout=15)

            if response.status_code != 200:
                _LOGGER.error(
                    "Login failed. Status: %s, Response: %s",
                    response.status_code,
                    response.text,
                )
                raise TimeTreeAuthError("Invalid credentials")

            self._session_id = response.cookies.get("_session_id")
            _LOGGER.debug("Login successful.")
        except TimeTreeAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("Login connection error: %s", e)
            raise TimeTreeAuthError(f"Connection error: {e}") from e

        self._resolve_user_id()
        return True

    def _resolve_user_id(self):
        """Fetch the account's numeric user id, needed for create_event's
        `attendees` field (TimeTree does not add the creator automatically
        server-side; the web client does this client-side before posting).
        """
        try:
            url = f"{API_BASEURI}/user"
            response = self._session.get(
                url, headers={"X-Timetreea": API_USER_AGENT}, timeout=15
            )
            response.raise_for_status()
            self._user_id = response.json().get("user", {}).get("id")
            _LOGGER.debug("Resolved TimeTree user id: %s", self._user_id)
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning(
                "Could not resolve TimeTree user id (create_event may fail): %s", e
            )
            self._user_id = None

    # ------------------------------------------------------------------ #
    # Calendars / Labels
    # ------------------------------------------------------------------ #

    def _get_calendars(self):
        """Get list of calendars."""
        if not self._session_id:
            self._login()

        url = f"{API_BASEURI}/calendars?since=0"
        headers = {"X-Timetreea": API_USER_AGENT}

        response = self._session.get(url, headers=headers, timeout=15)

        if response.status_code == 401:
            _LOGGER.debug("Token expired during calendar fetch. Re-logging in.")
            self._login()
            response = self._session.get(url, headers=headers, timeout=15)

        response.raise_for_status()
        data = response.json()
        return [
            {
                "id": c["id"],
                "name": c["name"],
                "alias_code": c.get("alias_code"),
            }
            for c in data.get("calendars", [])
            if c.get("deactivated_at") is None
        ]

    def _get_labels(self, calendar_id):
        """Get the label definitions (id -> name/color) for a calendar.

        Powers per-label colour/name attributes on calendar events
        (GitHub issue #4) and supplies a default `label_id` for
        create_event, since every TimeTree event requires one.
        """
        url = f"{API_BASEURI}/calendar/{calendar_id}/labels"
        headers = {"X-Timetreea": API_USER_AGENT}

        try:
            response = self._session.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            r_json = response.json()
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("Could not fetch labels for calendar %s: %s", calendar_id, e)
            return {}

        labels = {}
        for label in r_json.get("calendar_labels", []):
            label_id = label.get("id")
            if label_id is None:
                continue
            color = label.get("color")
            if isinstance(color, int):
                color = f"#{color:06x}"
            labels[label_id] = {
                "name": label.get("name", ""),
                "color": color,
            }
        return labels

    # ------------------------------------------------------------------ #
    # Events (read)
    # ------------------------------------------------------------------ #

    def _get_events_recur(self, calendar_id, since):
        """Recursive fetch for events if the API response is chunked."""
        url = f"{API_BASEURI}/calendar/{calendar_id}/events/sync?since={since}"
        headers = {"X-Timetreea": API_USER_AGENT}

        response = self._session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        r_json = response.json()

        events = r_json.get("events", [])
        if r_json.get("chunk") is True:
            events.extend(self._get_events_recur(calendar_id, r_json["since"]))

        return events

    def _get_events(self, calendar_id):
        """Fetch all events for a specific calendar."""
        if not self._session_id:
            self._login()

        url = f"{API_BASEURI}/calendar/{calendar_id}/events/sync"
        headers = {"X-Timetreea": API_USER_AGENT}

        response = self._session.get(url, headers=headers, timeout=30)

        if response.status_code == 401:
            _LOGGER.debug("Token expired during event fetch. Re-logging in.")
            self._login()
            response = self._session.get(url, headers=headers, timeout=30)

        response.raise_for_status()
        r_json = response.json()

        events = r_json.get("events", [])
        if r_json.get("chunk") is True:
            events.extend(self._get_events_recur(calendar_id, r_json["since"]))

        _LOGGER.debug("Fetched %s raw events.", len(events))
        return events

    # ------------------------------------------------------------------ #
    # Events (write) - see module docstring for the four defects fixed here
    # ------------------------------------------------------------------ #

    def _scrape_csrf_token(self, calendar_alias):
        """Scrape a fresh CSRF token from a calendar page (Defect 3).

        Not (per issue #5) a single-use nonce, but we fetch a fresh one on
        every create_event call anyway - it's a cheap GET and avoids any
        risk of an expired/stale token.
        """
        url = f"{WEB_BASEURI}/calendars/{calendar_alias}/events/new"
        response = self._session.get(
            url, headers={"X-Timetreea": API_USER_AGENT}, timeout=15
        )
        response.raise_for_status()
        match = _CSRF_META_RE.search(response.text)
        if not match:
            raise TimeTreeApiError(
                "Could not find CSRF token on calendar page - TimeTree's page "
                "structure may have changed."
            )
        return match.group(1)

    @staticmethod
    def _all_day_to_utc_midnight_ms(d: date) -> int:
        """Convert an all-day date to a UTC-midnight ms timestamp.

        TimeTree's own all-day timestamps are always exact multiples of
        86400s in UTC; using the local timezone would shift the date by one
        day for any timezone with a positive UTC offset (Defect 4).
        """
        dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    def _create_event(
        self,
        calendar_id,
        calendar_alias,
        summary,
        description,
        location,
        all_day,
        dt_start,
        dt_end,
        label_id,
    ):
        """Create a new event in TimeTree."""
        if not self._session_id:
            self._login()

        if self._user_id is None:
            self._resolve_user_id()

        if all_day:
            # HA Core provides an *exclusive* dtend (RFC5545 convention: the
            # day after the actual last day). TimeTree expects an
            # *inclusive* end_at (midnight of the actual last day itself) -
            # a real 1-day TimeTree event has start_at == end_at.
            # (Defect 4, write side.)
            inclusive_end_date = dt_end - timedelta(days=1)
            start_ms = self._all_day_to_utc_midnight_ms(dt_start)
            end_ms = self._all_day_to_utc_midnight_ms(inclusive_end_date)
            tz_name = "UTC"
        else:
            start_ms = int(dt_start.timestamp() * 1000)
            end_ms = int(dt_end.timestamp() * 1000)
            tz_name = str(dt_start.tzinfo) if dt_start.tzinfo else "UTC"

        payload = {
            "title": summary or "New Event",
            "all_day": all_day,
            "start_at": start_ms,
            "start_timezone": tz_name,
            "end_at": end_ms,
            "end_timezone": tz_name,
            "label_id": label_id,
            "note": description or "",
            "location": location or "",
            "attendees": [self._user_id] if self._user_id else [],
            "recurrences": [],
            "alerts": [],
            "attachment": {"virtual_user_attendees": []},
            "category": 1,
        }

        csrf_token = self._scrape_csrf_token(calendar_alias)

        url = f"{API_BASEURI}/calendar/{calendar_id}/event"  # singular - Defect 2
        headers = {
            "Content-Type": "application/json",
            "X-Timetreea": API_USER_AGENT,
            "X-CSRF-Token": csrf_token,
            "Origin": WEB_BASEURI,
            "Referer": f"{WEB_BASEURI}/calendars/{calendar_alias}/events/new",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        _LOGGER.debug("Creating TimeTree event, payload: %s", payload)

        response = self._session.post(url, json=payload, headers=headers, timeout=15)

        if response.status_code == 401:
            _LOGGER.debug("Token expired during create_event. Re-logging in.")
            self._login()
            csrf_token = self._scrape_csrf_token(calendar_alias)
            headers["X-CSRF-Token"] = csrf_token
            response = self._session.post(url, json=payload, headers=headers, timeout=15)

        if response.status_code not in (200, 201):
            _LOGGER.error(
                "Failed to create event. Status: %s, Body: %s",
                response.status_code,
                response.text,
            )
            raise TimeTreeApiError(f"API Error {response.status_code}: {response.text}")

        return response.json()

    # ------------------------------------------------------------------ #
    # Async wrappers
    # ------------------------------------------------------------------ #

    async def async_validate_and_get_calendars(self):
        return await self._hass.async_add_executor_job(self._do_validate)

    def _do_validate(self):
        self._login()
        return self._get_calendars()

    async def async_get_events(self, calendar_id):
        return await self._hass.async_add_executor_job(self._get_events, calendar_id)

    async def async_get_labels(self, calendar_id):
        return await self._hass.async_add_executor_job(self._get_labels, calendar_id)

    async def async_create_event(
        self,
        calendar_id,
        calendar_alias,
        summary,
        description,
        location,
        all_day,
        dt_start,
        dt_end,
        label_id,
    ):
        return await self._hass.async_add_executor_job(
            self._create_event,
            calendar_id,
            calendar_alias,
            summary,
            description,
            location,
            all_day,
            dt_start,
            dt_end,
            label_id,
        )

    # ------------------------------------------------------------------ #
    # Shared parsing helper (used by ics_builder.py too)
    # ------------------------------------------------------------------ #

    @staticmethod
    def convert_ts(ts, tz_name):
        """Convert a TimeTree millisecond timestamp to an aware datetime."""
        try:
            if ts >= 0:
                return datetime.fromtimestamp(ts / 1000, ZoneInfo(tz_name))
            return datetime.fromtimestamp(0, ZoneInfo(tz_name)) + timedelta(
                seconds=int(ts / 1000)
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not parse timestamp %s / tz %s", ts, tz_name)
            return datetime.now(ZoneInfo("UTC"))

    @staticmethod
    def parse_event(event_data):
        """Parse a raw TimeTree event dict into our internal representation.

        Handles the read-side mirror of Defect 4: TimeTree's all-day end_at
        is inclusive (midnight of the last actual day); Home Assistant's
        CalendarEvent expects an exclusive end, so one day is added back.
        """
        start_ts = event_data.get("start_at", 0)
        end_ts = event_data.get("end_at", 0)
        start_tz = event_data.get("start_timezone") or "UTC"
        end_tz = event_data.get("end_timezone") or start_tz
        all_day = bool(event_data.get("all_day"))

        start_dt = TimeTreeApi.convert_ts(start_ts, start_tz)
        end_dt = TimeTreeApi.convert_ts(end_ts, end_tz)

        if all_day:
            start_val = start_dt.date()
            # Defect 4, read side: add one day back (inclusive -> exclusive)
            end_val = end_dt.date() + timedelta(days=1)
        else:
            start_val = start_dt
            end_val = end_dt

        return {
            "uid": event_data.get("uuid"),
            "summary": event_data.get("title") or "Ohne Titel",
            "start": start_val,
            "end": end_val,
            "all_day": all_day,
            "location": event_data.get("location"),
            "description": event_data.get("note"),
            "recurrences": event_data.get("recurrences"),
            "label_id": event_data.get("label_id"),
            "deleted_at": event_data.get("deleted_at"),
            "updated_at": event_data.get("updated_at"),
        }
