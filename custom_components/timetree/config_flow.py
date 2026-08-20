"""Config flow for TimeTree integration."""
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import selector

from .api import TimeTreeApi, TimeTreeAuthError
from .const import (
    CONF_CALENDAR_ALIAS,
    CONF_CALENDAR_ID,
    CONF_CALENDAR_NAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class TimeTreeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for TimeTree."""

    VERSION = 1

    def __init__(self):
        self._api = None
        self._calendars = None
        self._auth_data = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TimeTreeOptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        """Step 1: email and password."""
        errors = {}

        if user_input is not None:
            self._auth_data = user_input
            self._api = TimeTreeApi(
                self.hass, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )

            try:
                self._calendars = await self._api.async_validate_and_get_calendars()
                return await self.async_step_calendar()
            except TimeTreeAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating TimeTree credentials")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
                    ),
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_calendar(self, user_input=None):
        """Step 2: pick a calendar and the poll interval."""
        errors = {}

        try:
            if user_input is not None:
                selected_cal_id = user_input[CONF_CALENDAR_ID]
                selected_cal_name = "TimeTree Kalender"
                selected_cal_alias = None

                if self._calendars:
                    for c in self._calendars:
                        if str(c["id"]) == selected_cal_id:
                            selected_cal_name = c["name"]
                            selected_cal_alias = c.get("alias_code")
                            break

                await self.async_set_unique_id(f"{DOMAIN}_{selected_cal_id}")
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=selected_cal_name,
                    data={
                        **self._auth_data,
                        CONF_CALENDAR_ID: selected_cal_id,
                        CONF_CALENDAR_NAME: selected_cal_name,
                        CONF_CALENDAR_ALIAS: selected_cal_alias,
                        CONF_SCAN_INTERVAL: user_input.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    },
                )

            if not self._calendars:
                return self.async_abort(reason="no_calendars_found")

            calendar_options = [
                {"value": str(c["id"]), "label": c["name"]} for c in self._calendars
            ]

            schema = vol.Schema(
                {
                    vol.Required(CONF_CALENDAR_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=calendar_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=1,
                            mode=selector.NumberSelectorMode.SLIDER,
                        )
                    ),
                }
            )

            return self.async_show_form(step_id="calendar", data_schema=schema, errors=errors)

        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error in calendar selection step")
            return self.async_abort(reason="unknown")


class TimeTreeOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle TimeTree options (poll interval)."""

    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self._config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self._config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=current_interval
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=1,
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                )
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
