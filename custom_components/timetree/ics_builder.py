"""Build a valid icalendar.Calendar from raw TimeTree event dicts.

TimeTree's `recurrences` field returns a list of raw ICS content lines,
e.g. ["RRULE:FREQ=YEARLY"], sometimes together with EXDATE/RDATE lines.
This must be parsed with icalendar's own content-line parser rather than
forwarded as a plain string.

This is the piece that was missing in the original
acdcnow/Timetree-Import-for-Home-Assistant integration and is the root
cause of its "rrule did not contain FREQ" crash (GitHub issue #2): it
passed the raw "RRULE:FREQ=..." string - including the "RRULE:"
property-name prefix - straight into Home Assistant's CalendarEvent,
which expects only the bare value ("FREQ=...").

Here, recurring events are expanded ourselves ahead of time (see
coordinator.py) using the `recurring_ical_events` library, so Home
Assistant never has to parse a raw rrule string at all - it only ever
sees plain, already-resolved event occurrences.
"""
import logging

from icalendar import Calendar, Event
from icalendar.parser import Contentline

from .api import TimeTreeApi

_LOGGER = logging.getLogger(__name__)

_RECOGNIZED_RECURRENCE_PROPERTIES = ("rrule", "exdate", "rdate")


def _dtstart_as_ics(dt) -> str:
    """Format a date/datetime as an ICS DTSTART value for the wrapper below."""
    if hasattr(dt, "hour"):
        return dt.strftime("%Y%m%dT%H%M%SZ")
    return dt.strftime("%Y%m%d")


def _add_recurrences(event: Event, recurrences: list):
    """Parse TimeTree's raw recurrence content lines onto an icalendar Event.

    Each raw line (e.g. "RRULE:FREQ=YEARLY") is wrapped in a minimal
    throwaway VEVENT and parsed via icalendar's own `Calendar.from_ical`,
    which correctly separates the property name from its value. This is
    the most version-stable way to parse a single content line with
    icalendar, avoiding brittle internal APIs.
    """
    if not recurrences:
        return

    dtstart_ics = _dtstart_as_ics(event.get("dtstart").dt)

    for line in recurrences:
        try:
            name, _params, _value = Contentline(line).parts()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Skipping unparsable recurrence line: %s", line)
            continue

        if name.lower() not in _RECOGNIZED_RECURRENCE_PROPERTIES:
            _LOGGER.debug("Unknown recurrence property, skipping: %s", name)
            continue

        wrapper = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:tmp\r\n"
            f"DTSTART:{dtstart_ics}\r\n"
            f"{line}\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        try:
            parsed_event = Calendar.from_ical(wrapper).walk("VEVENT")[0]
            prop_key = name.upper()
            if prop_key in parsed_event:
                event[prop_key] = parsed_event[prop_key]
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not apply recurrence line '%s': %s", line, err)


def build_calendar(raw_events: list) -> Calendar:
    """Convert a list of raw TimeTree event dicts into an icalendar.Calendar."""
    cal = Calendar()
    cal.add("prodid", "-//Home Assistant TimeTree Read-Only//")
    cal.add("version", "2.0")

    for raw in raw_events:
        if raw.get("deleted_at"):
            continue

        uid = raw.get("uuid")
        title = raw.get("title") or "Ohne Titel"
        all_day = bool(raw.get("all_day"))
        start_tz = raw.get("start_timezone") or "UTC"
        end_tz = raw.get("end_timezone") or start_tz

        start_dt = TimeTreeApi.convert_ts(raw.get("start_at", 0), start_tz)
        end_dt = TimeTreeApi.convert_ts(raw.get("end_at", 0), end_tz)

        event = Event()
        event.add("uid", uid)
        event.add("summary", title)
        if raw.get("note"):
            event.add("description", raw["note"])
        if raw.get("location"):
            event.add("location", raw["location"])
        if raw.get("label_id") is not None:
            # Custom X- property so the label survives recurrence expansion
            # (recurring_ical_events copies all properties onto each
            # occurrence); read back in coordinator.py to attach the
            # label's name/color as entity attributes (GitHub issue #4).
            event.add("x-timetree-label-id", str(raw["label_id"]))

        if all_day:
            event.add("dtstart", start_dt.date())
            event.add("dtend", end_dt.date())
        else:
            event.add("dtstart", start_dt)
            event.add("dtend", end_dt)

        _add_recurrences(event, raw.get("recurrences"))

        cal.add_component(event)

    return cal
