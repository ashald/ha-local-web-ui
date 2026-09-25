"""Config, options and web UI (subentry) flows for Local Web UIs."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    IconSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .const import (
    CONF_DEVICE_ID,
    CONF_DISCOVERY,
    CONF_ICON,
    CONF_LINK_DEVICE_PAGES,
    CONF_LINKED_DEVICES,
    CONF_MODE,
    CONF_PANEL_ICON,
    CONF_PANEL_TITLE,
    CONF_PASSWORD,
    CONF_SHOW_IN_SIDEBAR,
    CONF_TRUSTED_ACK,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_DISCOVERY,
    DEFAULT_LINK_DEVICE_PAGES,
    DEFAULT_LINKED_DEVICES,
    DEFAULT_PANEL_ICON,
    DOMAIN,
    MODE_ISOLATED,
    MODE_TRUSTED,
    MODES,
    NAME,
    SUBENTRY_TYPE_VIEW,
)
from .discovery import parse_http_url

CONF_NAME = "name"


class LocalWebUiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single instance; web UIs are added as subentries."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=NAME,
                data={},
                options={
                    CONF_DISCOVERY: DEFAULT_DISCOVERY,
                    CONF_LINK_DEVICE_PAGES: DEFAULT_LINK_DEVICE_PAGES,
                    CONF_LINKED_DEVICES: DEFAULT_LINKED_DEVICES,
                },
            )
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> LocalWebUiOptionsFlow:
        return LocalWebUiOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_VIEW: WebUiSubentryFlow}


class LocalWebUiOptionsFlow(OptionsFlow):
    """Global switches."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            # Left empty: the default name and icon
            return self.async_create_entry(data={k: v for k, v in user_input.items() if v != ""})
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DISCOVERY,
                        default=options.get(CONF_DISCOVERY, DEFAULT_DISCOVERY),
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_LINK_DEVICE_PAGES,
                        default=options.get(CONF_LINK_DEVICE_PAGES, DEFAULT_LINK_DEVICE_PAGES),
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_LINKED_DEVICES,
                        default=options.get(CONF_LINKED_DEVICES, DEFAULT_LINKED_DEVICES),
                    ): BooleanSelector(),
                    vol.Optional(
                        CONF_PANEL_TITLE,
                        description={"suggested_value": options.get(CONF_PANEL_TITLE)},
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                    vol.Optional(
                        CONF_PANEL_ICON,
                        description={
                            "suggested_value": options.get(CONF_PANEL_ICON, DEFAULT_PANEL_ICON)
                        },
                    ): IconSelector(),
                }
            ),
        )


def _view_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, vol.UNDEFINED)): TextSelector(),
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, vol.UNDEFINED)): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            vol.Required(CONF_MODE, default=defaults.get(CONF_MODE, MODE_ISOLATED)): SelectSelector(
                SelectSelectorConfig(
                    options=list(MODES),
                    mode=SelectSelectorMode.LIST,
                    translation_key=CONF_MODE,
                )
            ),
            vol.Required(
                CONF_TRUSTED_ACK, default=defaults.get(CONF_TRUSTED_ACK, False)
            ): BooleanSelector(),
            vol.Required(
                CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)
            ): BooleanSelector(),
            vol.Optional(
                CONF_USERNAME,
                description={"suggested_value": defaults.get(CONF_USERNAME)},
            ): TextSelector(TextSelectorConfig(autocomplete="off")),
            # On reconfigure an empty password keeps the stored one
            vol.Optional(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off")
            ),
            vol.Required(
                CONF_SHOW_IN_SIDEBAR, default=defaults.get(CONF_SHOW_IN_SIDEBAR, False)
            ): BooleanSelector(),
            vol.Optional(
                CONF_ICON, description={"suggested_value": defaults.get(CONF_ICON)}
            ): IconSelector(),
        }
    )


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not user_input.get(CONF_NAME, "").strip():
        errors[CONF_NAME] = "name_required"
    if parse_http_url(user_input.get(CONF_URL, "").strip()) is None:
        errors[CONF_URL] = "invalid_url"
    if user_input.get(CONF_MODE) == MODE_TRUSTED and not user_input.get(CONF_TRUSTED_ACK):
        errors[CONF_TRUSTED_ACK] = "trusted_not_acknowledged"
    return errors


class WebUiSubentryFlow(ConfigSubentryFlow):
    """Add or edit one web UI."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and not (errors := _validate(user_input)):
            return self.async_create_entry(
                title=user_input[CONF_NAME].strip(), data=_subentry_data(user_input, {})
            )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_view_schema({}), user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None and not (errors := _validate(user_input)):
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=user_input[CONF_NAME].strip(),
                data=_subentry_data(user_input, dict(subentry.data)),
            )
        defaults = {CONF_NAME: subentry.title, **subentry.data}
        schema = _view_schema(defaults)
        if user_input is not None:
            # Keep what was typed when validation fails, except the password
            schema = self.add_suggested_values_to_schema(
                schema, {k: v for k, v in user_input.items() if k != CONF_PASSWORD}
            )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)


def _subentry_data(user_input: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    url = parse_http_url(user_input[CONF_URL].strip())
    assert url is not None  # Validated before
    username = (user_input.get(CONF_USERNAME) or "").strip()
    password = user_input.get(CONF_PASSWORD) or previous.get(CONF_PASSWORD)
    if url.user is not None or url.password is not None:
        # Credentials typed into the URL: keep them where they are used (and redacted)
        username = username or url.user or ""
        password = user_input.get(CONF_PASSWORD) or url.password or password
        url = url.with_user(None)
    data: dict[str, Any] = {
        CONF_URL: str(url),
        CONF_MODE: user_input[CONF_MODE],
        CONF_TRUSTED_ACK: bool(user_input.get(CONF_TRUSTED_ACK)),
        CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
        CONF_SHOW_IN_SIDEBAR: user_input[CONF_SHOW_IN_SIDEBAR],
    }
    if username:
        data[CONF_USERNAME] = username
        if password:
            data[CONF_PASSWORD] = password
    if user_input.get(CONF_ICON):
        data[CONF_ICON] = user_input[CONF_ICON]
    if previous.get(CONF_DEVICE_ID):
        data[CONF_DEVICE_ID] = previous[CONF_DEVICE_ID]
    return data
