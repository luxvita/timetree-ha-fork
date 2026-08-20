"""Constants for the TimeTree integration."""

DOMAIN = "timetree"

CONF_CALENDAR_ID = "calendar_id"
CONF_CALENDAR_NAME = "calendar_name"
CONF_CALENDAR_ALIAS = "calendar_alias"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL = 15  # minutes
MIN_SCAN_INTERVAL = 1
MAX_SCAN_INTERVAL = 120

# How far into the past / future occurrences of recurring events
# (birthdays, anniversaries, ...) are expanded on every refresh.
PAST_WINDOW_DAYS = 14
FUTURE_WINDOW_DAYS = 400

# Browser TLS fingerprint impersonated for the create-event endpoint, which
# sits behind a WAF/anti-bot layer that checks the TLS ClientHello (JA3/JA4)
# independently of HTTP-level headers (see GitHub issue #5, "Defect 3").
CURL_CFFI_IMPERSONATE = "firefox135"

LOGGER_NAME = "custom_components.timetree"
