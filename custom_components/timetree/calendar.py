"""Calendar platform for TimeTree.

Read side: works on already-expanded occurrences from the coordinator (see
coordinator.py) - no raw rrule ever reaches Home Assistant, which is the
fix for GitHub issues #1 and #2.

Write side: async_create_event implements the fixes from GitHub issues #3
and #5 (correct kwargs, correct endpoint/payload, CSRF + TLS fingerprint,
correct all-day date handling) - see api.py's module docstring for detail
on each defect.

Per-label entities: Home Assistant's CalendarEvent has no per-event colour
field, and neither the native iOS calendar app nor common Lovelace
calendar cards (incl. Calendar Card Pro) render one - colour is always a
property of the *calendar entity*, never of an individual event (the same
constraint applies to CalDAV/iOS, see project notes). Since TimeTree's own
per-event colour is itself just its label's colour (confirmed against
timetree-exporter's source), the only way to get TimeTree-equivalent
colours in Home Assistant is one calendar entity per label - exactly the
same "one calendar per person" pattern used for the Nextcloud/CalDAV
migration. This module therefore sets up one entity per known label (plus
one "unlabeled" bucket) *in addition to* the original combined entity, so
existing dashboards/automations referencing the combined entity keep
working unchanged.
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

# Sentinel used as this entity's label filter to mean "show every event,
# regardless of label" (the original, combined entity).
_ALL_LABELS = object()
# Sentinel used to mean "only events with no label assigned".
_NO_LABEL = object()


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the calendar entities: one combined + one per TimeTree label."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    calendar_name = entry.data[CONF_CALENDAR_NAME]
    calendar_alias = entry.data[CONF_CALENDAR_ALIAS]

    entities = [
        TimeTreeCalendarEntity(
            coordinator,
            calendar_name,
            calendar_alias,
            label_filter=_ALL_LABELS,
            supports_create=True,
        )
    ]

    for label_id, info in coordinator.labels.items():
        label_name = info.get("name") or f"Label {label_id}"
        entities.append(
            TimeTreeCalendarEntity(
                coordinator,
                f"{calendar_name} - {label_name}",
                calendar_alias,
                label_filter=label_id,
            )
        )

    entities.append(
        TimeTreeCalendarEntity(
            coordinator,
            f"{calendar_name} - Unlabeled",
            calendar_alias,
            label_filter=_NO_LABEL,
        )
    )

    async_add_entities(entities)


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
    """Representation of a TimeTree calendar (optionally filtered by label)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TimeTreeCoordinator,
        name: str,
        calendar_alias: str,
        label_filter=_ALL_LABELS,
        supports_create: bool = False,
    ):
        self.coordinator = coordinator
        self._attr_name = name
        self._calendar_alias = calendar_alias
        self._label_filter = label_filter

        if label_filter is _ALL_LABELS:
            # Keep the exact same unique_id as before the per-label split,
            # so existing installs don't lose their entity's history/id.
            self._attr_unique_id = f"{DOMAIN}_{coordinator.calendar_id}"
        elif label_filter is _NO_LABEL:
            self._attr_unique_id = f"{DOMAIN}_{coordinator.calendar_id}_unlabeled"
        else:
            self._attr_unique_id = f"{DOMAIN}_{coordinator.calendar_id}_label_{label_filter}"

        if supports_create:
            self._attr_supported_features = CalendarEntityFeature.CREATE_EVENT

    # ------------------------------------------------------------------ #
    # Filtering
    # ------------------------------------------------------------------ #

    def _matching_events(self):
        events = self.coordinator.data or []
        if self._label_filter is _ALL_LABELS:
            return events
        if self._label_filter is _NO_LABEL:
            return [e for e in events if e.get("label_id") is None]
        return [e for e in events if e.get("label_id") == self._label_filter]

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    @property
    def event(self):
        """Return the next upcoming event (within this entity's label filter)."""
        now = dt_util.now()
        upcoming = [
            e for e in self._matching_events() if _as_comparable_datetime(e["end"]) > now
        ]
        if not upcoming:
            return None

        next_event = min(upcoming, key=lambda e: _as_comparable_datetime(e["start"]))
        return self._build_calendar_event(next_event)

    async def async_get_events(self, hass, start_date, end_date):
        """Return calendar events within a range (within this entity's label filter)."""
        if self.coordinator.data is None:
            await self.coordinator.async_request_refresh()

        events = []
        for event_data in self._matching_events():
            ev_start = _as_comparable_datetime(event_data["start"])
            ev_end = _as_comparable_datetime(event_data["end"])

            if ev_start < end_date and ev_end > start_date:
                events.append(self._build_calendar_event(event_data))

        return events

    @property
    def extra_state_attributes(self):
        """Expose this entity's TimeTree label colour/name (GitHub issue #4).

        Copy this "color" value into your Lovelace calendar card's
        per-entity colour setting to get TimeTree-equivalent colours - HA
        has no mechanism to apply this automatically, since calendar
        colouring is a card-configuration concern, not an entity-state
        concern (see module docstring).
        """
        if self._label_filter is _ALL_LABELS:
            return {
                "available_labels": {
                    str(lid): {"name": info.get("name"), "color": info.get("color")}
                    for lid, info in self.coordinator.labels.items()
                }
            }
        if self._label_filter is _NO_LABEL:
            return {"label_name": "Unlabeled", "color": None}

        info = self.coordinator.labels.get(self._label_filter, {})
        return {
            "label_id": self._label_filter,
            "label_name": info.get("name"),
            "color": info.get("color"),
        }

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
    # Write (only enabled on the combined _ALL_LABELS entity)
    # ------------------------------------------------------------------ #

    async def async_create_event(self, **kwargs):
        """Add a new event to TimeTree.

        Home Assistant Core's calendar service layer normalises
        create_event data into "dtstart"/"dtend" (datetime for timed
        events, date for all-day) before calling this method - NOT
        "start_date_time"/"start_date" as the original integration
        assumed (GitHub issue #3).

        Note: editing or deleting existing events is not supported here -
        do that in the TimeTree app; changes are picked up on the next
        poll (or immediately after a create_event call, since that
        triggers an immediate refresh).
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
