"""Calendar platform for TimeTree.

Read side: works on already-expanded occurrences from the coordinator (see
coordinator.py) - no raw rrule ever reaches Home Assistant, which is the
fix for GitHub issues #1 and #2.

Write side: async_create_event implements the fixes from GitHub issues #3
and #5 (correct kwargs, correct endpoint/payload, CSRF + TLS fingerprint,
correct all-day date handling) - see api.py's module docstring for detail
on each defect.
"""
from datetime import date, datetime
import logging

from homeassistant.components.calendar import CalendarEntity, CalendarEntityFeature, CalendarEvent
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .api import TimeTreeApiError
from .const import CONF_CALENDAR_ALIAS, CONF_CALENDAR_NAME, DOMAIN
from .coordinator import TimeTreeCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the calendar entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entity = TimeTreeCalendarEntity(
        coordinator,
        entry.data[CONF_CALENDAR_NAME],
        entry.data[CONF_CALENDAR_ALIAS],
    )
    async_add_entities([entity])


def _as_comparable_datetime(value):
    """Normalize a date/datetime into an aware datetime for comparisons."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return value
    if isinstance(value, date):
        return dt_util.start_of_local_day(datetime.combine(value, datetime.min.time()))
    return value


class TimeTreeCalendarEntity(CalendarEntity):
    """Representation of a TimeTree calendar."""

    _attr_has_entity_name = True
    _attr_supported_features = CalendarEntityFeature.CREATE_EVENT

    def __init__(self, coordinator: TimeTreeCoordinator, name: str, calendar_alias: str):
        self.coordinator = coordinator
        self._attr_name = name
        self._calendar_alias = calendar_alias
        self._attr_unique_id = f"{DOMAIN}_{coordinator.calendar_id}"

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    @property
    def event(self):
        """Return the next upcoming event."""
        now = dt_util.now()
        events = self.coordinator.data or []

        upcoming = [e for e in events if _as_comparable_datetime(e["end"]) > now]
        if not upcoming:
            return None

        next_event = min(upcoming, key=lambda e: _as_comparable_datetime(e["start"]))
        return self._build_calendar_event(next_event)

    async def async_get_events(self, hass, start_date, end_date):
        """Return calendar events within a range."""
        if self.coordinator.data is None:
            await self.coordinator.async_request_refresh()

        events = []
        for event_data in self.coordinator.data or []:
            ev_start = _as_comparable_datetime(event_data["start"])
            ev_end = _as_comparable_datetime(event_data["end"])

            if ev_start < end_date and ev_end > start_date:
                events.append(self._build_calendar_event(event_data))

        return events

    @property
    def extra_state_attributes(self):
        """Expose TimeTree label info for the *next* event (GitHub issue #4).

        HA's CalendarEvent dataclass only serialises its own declared
        fields (summary/start/end/location/description/uid) via
        `as_dict()` - anything else attached to it is silently dropped and
        never reaches the frontend or `calendar.get_events`. Per-event
        colour also isn't rendered by the native iOS calendar app or common
        Lovelace calendar cards (incl. Calendar Card Pro) regardless.
        `extra_state_attributes` is the correct, idiomatic place to expose
        this kind of extra metadata on a HA entity: it shows up in
        Developer Tools > States and via `state_attr()` in templates, so a
        custom card/automation can still make use of it.

        Also lists all known labels for the calendar for reference.
        """
        attrs = {
            "available_labels": {
                str(lid): {"name": info.get("name"), "color": info.get("color")}
                for lid, info in self.coordinator.labels.items()
            }
        }

        events = self.coordinator.data or []
        now = dt_util.now()
        upcoming = [e for e in events if _as_comparable_datetime(e["end"]) > now]
        if upcoming:
            next_event = min(upcoming, key=lambda e: _as_comparable_datetime(e["start"]))
            attrs["next_event_label_id"] = next_event.get("label_id")
            attrs["next_event_label_name"] = next_event.get("label_name")
            attrs["next_event_label_color"] = next_event.get("label_color")

        return attrs

    def _build_calendar_event(self, event_data):
        return CalendarEvent(
            summary=event_data["summary"],
            start=event_data["start"],
            end=event_data["end"],
            location=event_data["location"],
            description=event_data["description"],
            uid=event_data["uid"],
        )

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    async def async_create_event(self, **kwargs):
        """Add a new event to TimeTree.

        Home Assistant Core's calendar service layer normalises
        create_event data into "dtstart"/"dtend" (datetime for timed
        events, date for all-day) before calling this method - NOT
        "start_date_time"/"start_date" as the original integration
        assumed (GitHub issue #3).
        """
        summary = kwargs.get("summary", "New Event")
        description = kwargs.get("description", "")
        location = kwargs.get("location", "")

        start = kwargs.get("dtstart")
        end = kwargs.get("dtend")
        if start is None:
            # Defensive fallback for older HA core versions / direct calls
            start = kwargs.get("start_date_time") or kwargs.get("start_date")
            end = kwargs.get("end_date_time") or kwargs.get("end_date")

        if start is None:
            raise HomeAssistantError(
                "TimeTree: no start date/time received from Home Assistant."
            )

        all_day = not isinstance(start, datetime)

        default_label_id = next(iter(self.coordinator.labels), None)
        if default_label_id is None:
            _LOGGER.warning(
                "No TimeTree labels found for this calendar; create_event will "
                "likely be rejected (TimeTree requires every event to have a "
                "label_id)."
            )

        try:
            await self.coordinator.api.async_create_event(
                calendar_id=self.coordinator.calendar_id,
                calendar_alias=self._calendar_alias,
                summary=summary,
                description=description,
                location=location,
                all_day=all_day,
                dt_start=start,
                dt_end=end,
                label_id=default_label_id,
            )
            await self.coordinator.async_request_refresh()
        except TimeTreeApiError as err:
            _LOGGER.error("Error creating TimeTree event: %s", err)
            raise HomeAssistantError(f"TimeTree API rejected the event: {err}") from err
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Unexpected error creating TimeTree event")
            raise HomeAssistantError(f"TimeTree event creation failed: {err}") from err
