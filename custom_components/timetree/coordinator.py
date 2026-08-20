"""DataUpdateCoordinator for TimeTree.

Fetches raw events and label definitions, then expands recurring events
into concrete occurrences ahead of time (see ics_builder.py / module
docstring there for why - this is the fix for GitHub issues #1 and #2).
"""
import logging
from datetime import date, datetime, timedelta

import recurring_ical_events

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TimeTreeApi
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FUTURE_WINDOW_DAYS,
    PAST_WINDOW_DAYS,
)
from .ics_builder import build_calendar

_LOGGER = logging.getLogger(__name__)


class TimeTreeCoordinator(DataUpdateCoordinator):
    """Fetches TimeTree events/labels and expands recurring events."""

    def __init__(self, hass, api: TimeTreeApi, calendar_id, calendar_alias, entry):
        interval_minutes = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=interval_minutes),
        )
        self.api = api
        self.calendar_id = calendar_id
        self.calendar_alias = calendar_alias
        self.entry = entry
        self.last_update_success_time = None
        self.labels = {}  # label_id -> {"name": ..., "color": ...}

    async def _async_update_data(self):
        """Fetch raw events + labels and expand into concrete occurrences."""
        try:
            raw_events = await self.api.async_get_events(self.calendar_id)
            self.labels = await self.api.async_get_labels(self.calendar_id)
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Error communicating with TimeTree API: {err}") from err

        # TimeTree's /labels endpoint has been observed to return an empty
        # "name" for every label on some accounts (the app appears to
        # resolve display names through a different, not-yet-understood
        # mechanism). Rather than guess, let the user supply the names
        # themselves once via the integration's options - see
        # config_flow.py's options flow.
        for label_id, info in self.labels.items():
            override_name = self.entry.options.get(f"label_name_{label_id}")
            if override_name:
                info["name"] = override_name

        try:
            parsed_events = await self.hass.async_add_executor_job(
                self._expand_events, raw_events, self.labels
            )
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Error processing TimeTree events: {err}") from err

        self.last_update_success_time = dt_util.now()
        return parsed_events

    @staticmethod
    def _expand_events(raw_events, labels):
        """Build an in-memory calendar and expand it to a flat occurrence list."""
        cal = build_calendar(raw_events)

        today = date.today()
        window_start = today - timedelta(days=PAST_WINDOW_DAYS)
        window_end = today + timedelta(days=FUTURE_WINDOW_DAYS)

        occurrences = recurring_ical_events.of(cal).between(window_start, window_end)

        events = []
        for occ in occurrences:
            dtstart = occ.get("dtstart").dt
            dtend_prop = occ.get("dtend")
            dtend = dtend_prop.dt if dtend_prop is not None else dtstart

            all_day = not isinstance(dtstart, datetime)

            base_uid = str(occ.get("uid"))
            occurrence_suffix = (
                dtstart.isoformat() if all_day else dtstart.date().isoformat()
            )
            uid = f"{base_uid}-{occurrence_suffix}"

            label_id_raw = occ.get("x-timetree-label-id")
            label_id = None
            if label_id_raw is not None:
                try:
                    label_id = int(str(label_id_raw))
                except (TypeError, ValueError):
                    label_id = None
            label_info = labels.get(label_id, {}) if label_id is not None else {}

            events.append(
                {
                    "uid": uid,
                    "base_uid": base_uid,
                    "summary": str(occ.get("summary") or "Ohne Titel"),
                    "start": dtstart,
                    "end": dtend,
                    "all_day": all_day,
                    "location": str(occ.get("location")) if occ.get("location") else None,
                    "description": str(occ.get("description"))
                    if occ.get("description")
                    else None,
                    "label_id": label_id,
                    "label_name": label_info.get("name"),
                    "label_color": label_info.get("color"),
                }
            )

        events.sort(
            key=lambda e: e["start"].isoformat()
            if hasattr(e["start"], "isoformat")
            else str(e["start"])
        )
        _LOGGER.debug(
            "Expanded %s raw events into %s occurrences (window: %s to %s)",
            len(raw_events),
            len(events),
            window_start,
            window_end,
        )
        return events
