"""Config and options flows for Local Web UIs.

The first entry is the hub (global options). After it, "Add entry" adds a web UI
by URL, and devices with a local web page are offered through discovery.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
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
    CONF_KIND,
    CONF_LINK_DEVICE_PAGES,
    CONF_LOCAL_DOMAINS,
    CONF_MODE,
    CONF_PANEL_ICON,
    CONF_PANEL_TITLE,
    CONF_PASSWORD,
    CONF_PREVIOUS_VIEW_ID,
    CONF_SHOW_IN_SIDEBAR,
    CONF_SHOW_PANEL,
    CONF_TRUSTED_ACK,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    CONF_VISIT_LINK,
    DEFAULT_DISCOVERY,
    DEFAULT_LINK_DEVICE_PAGES,
    DEFAULT_PANEL_ICON,
    DEFAULT_SHOW_PANEL,
    DOMAIN,
    HUB_UNIQUE_ID,
    KIND_HUB,
    KIND_VIEW,
    MODE_ISOLATED,
    MODE_TRUSTED,
    MODES,
    NAME,
    PANEL_URL_PATH,
    VISIT_CHOICES,
    VISIT_DEFAULT,
)
from .discovery import parse_http_url
from .hub import device_unique_id

CONF_NAME = "name"

HUB_DEFAULTS = {
    CONF_DISCOVERY: DEFAULT_DISCOVERY,
    CONF_LINK_DEVICE_PAGES: DEFAULT_LINK_DEVICE_PAGES,
    CONF_SHOW_PANEL: DEFAULT_SHOW_PANEL,
}


class LocalWebUiConfigFlow(ConfigFlow, domain=DOMAIN):
    """The hub first; then web UIs, added by URL or discovered."""

    VERSION = 2

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

    def _hub_exists(self) -> bool:
        return any(
            entry.data.get(CONF_KIND) == KIND_HUB
            for entry in self.hass.config_entries.async_entries(DOMAIN, include_ignore=False)
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._hub_exists():
            return await self.async_step_hub()
        return await self.async_step_web_ui()

    async def async_step_hub(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(HUB_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(
                title=NAME, data={CONF_KIND: KIND_HUB}, options=dict(HUB_DEFAULTS)
            )
        return self.async_show_form(step_id="hub")

    async def async_step_web_ui(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """A web UI added by URL: any page Home Assistant can reach."""
        errors: dict[str, str] = {}
        if user_input is not None and not (errors := _validate(user_input, manual=True)):
            return self.async_create_entry(
                title=user_input[CONF_NAME].strip(),
                data={CONF_KIND: KIND_VIEW},
                options=_view_options(user_input, {}, manual=True),
            )
        return self.async_show_form(
            step_id="web_ui",
            data_schema=self.add_suggested_values_to_schema(
                _view_schema(manual=True, device=False),
                {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD},
            ),
            errors=errors,
        )

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """A device whose integration links to a local web page."""
        await self.async_set_unique_id(device_unique_id(discovery_info[CONF_DEVICE_ID]))
        self._abort_if_unique_id_configured()
        self._discovered = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info["name"]}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        info = self._discovered
        if user_input is not None:
            return self.async_create_entry(
                title=info["name"],
                data={CONF_KIND: KIND_VIEW, CONF_DEVICE_ID: info[CONF_DEVICE_ID]},
                options={CONF_MODE: MODE_ISOLATED, CONF_VERIFY_SSL: False},
            )
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={
                "name": info["name"],
                "url": info[CONF_URL],
                "subtitle": info.get("subtitle") or "",
            },
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """A web UI of an older version (a subentry of the one entry)."""
        if device_id := import_data.get(CONF_DEVICE_ID):
            # Wins over the device being offered through discovery meanwhile
            await self.async_set_unique_id(device_unique_id(device_id), raise_on_progress=False)
            self._abort_if_unique_id_configured()
        data: dict[str, Any] = {
            CONF_KIND: KIND_VIEW,
            CONF_PREVIOUS_VIEW_ID: import_data[CONF_PREVIOUS_VIEW_ID],
        }
        options = dict(import_data["options"])
        if device_id:
            data[CONF_DEVICE_ID] = device_id
            options.pop(CONF_URL, None)  # Follows the device's own link
        return self.async_create_entry(title=import_data["title"], data=data, options=options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        if config_entry.data.get(CONF_KIND) == KIND_HUB:
            return HubOptionsFlow()
        return WebUiOptionsFlow()


class HubOptionsFlow(OptionsFlow):
    """Options for all web UIs."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_hub(user_input)

    async def async_step_hub(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            # Left empty: the default name and icon
            return self.async_create_entry(data={k: v for k, v in user_input.items() if v != ""})
        options = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(key, default=options.get(key, default)): BooleanSelector()
            for key, default in HUB_DEFAULTS.items()
        }
        schema[
            vol.Optional(
                CONF_PANEL_TITLE, description={"suggested_value": options.get(CONF_PANEL_TITLE)}
            )
        ] = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
        schema[
            vol.Optional(
                CONF_PANEL_ICON,
                description={"suggested_value": options.get(CONF_PANEL_ICON, DEFAULT_PANEL_ICON)},
            )
        ] = IconSelector()
        schema[
            vol.Optional(
                CONF_LOCAL_DOMAINS,
                description={"suggested_value": options.get(CONF_LOCAL_DOMAINS)},
            )
        ] = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
        return self.async_show_form(
            step_id="hub",
            data_schema=vol.Schema(schema),
            description_placeholders={"panel_url": f"/{PANEL_URL_PATH}"},
        )


class WebUiOptionsFlow(OptionsFlow):
    """Settings of one web UI."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_web_ui(user_input)

    async def async_step_web_ui(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        manual = not entry.data.get(CONF_DEVICE_ID)
        errors: dict[str, str] = {}
        if user_input is not None and not (errors := _validate(user_input, manual=manual)):
            # A web UI added by URL is renamed here too; others with HA's own rename
            if manual and (name := user_input[CONF_NAME].strip()) != entry.title:
                self.hass.config_entries.async_update_entry(entry, title=name)
            return self.async_create_entry(
                data=_view_options(user_input, dict(entry.options), manual=manual)
            )
        current: dict[str, Any] = {k: v for k, v in entry.options.items() if k != CONF_PASSWORD}
        if manual:
            current[CONF_NAME] = entry.title
        typed = {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD}
        return self.async_show_form(
            step_id="web_ui",
            data_schema=self.add_suggested_values_to_schema(
                _view_schema(manual=manual, device=not manual), {**current, **typed}
            ),
            errors=errors,
            description_placeholders={"panel_url": f"/{PANEL_URL_PATH}/{entry.entry_id}"},
        )


def _view_schema(manual: bool, device: bool) -> vol.Schema:
    schema: dict[Any, Any] = {}
    if manual:
        schema[vol.Required(CONF_NAME)] = TextSelector()
        schema[vol.Required(CONF_URL)] = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
    schema[vol.Required(CONF_MODE, default=MODE_ISOLATED)] = SelectSelector(
        SelectSelectorConfig(
            options=list(MODES), mode=SelectSelectorMode.LIST, translation_key=CONF_MODE
        )
    )
    schema[vol.Required(CONF_TRUSTED_ACK, default=False)] = BooleanSelector()
    if device:
        schema[vol.Required(CONF_VISIT_LINK, default=VISIT_DEFAULT)] = SelectSelector(
            SelectSelectorConfig(
                options=list(VISIT_CHOICES),
                mode=SelectSelectorMode.LIST,
                translation_key=CONF_VISIT_LINK,
            )
        )
    schema[vol.Required(CONF_VERIFY_SSL, default=manual)] = BooleanSelector()
    schema[vol.Optional(CONF_USERNAME)] = TextSelector(TextSelectorConfig(autocomplete="off"))
    # When editing, an empty password keeps the stored one
    schema[vol.Optional(CONF_PASSWORD)] = TextSelector(
        TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off")
    )
    schema[vol.Required(CONF_SHOW_IN_SIDEBAR, default=False)] = BooleanSelector()
    schema[vol.Optional(CONF_ICON)] = IconSelector()
    return vol.Schema(schema)


def _validate(user_input: dict[str, Any], manual: bool) -> dict[str, str]:
    errors: dict[str, str] = {}
    if manual:
        if not user_input.get(CONF_NAME, "").strip():
            errors[CONF_NAME] = "name_required"
        if parse_http_url(user_input.get(CONF_URL, "").strip()) is None:
            errors[CONF_URL] = "invalid_url"
    if user_input.get(CONF_MODE) == MODE_TRUSTED and not user_input.get(CONF_TRUSTED_ACK):
        errors[CONF_TRUSTED_ACK] = "trusted_not_acknowledged"
    return errors


def _view_options(
    user_input: dict[str, Any], previous: dict[str, Any], manual: bool
) -> dict[str, Any]:
    username = (user_input.get(CONF_USERNAME) or "").strip()
    password = user_input.get(CONF_PASSWORD) or previous.get(CONF_PASSWORD)
    options: dict[str, Any] = {
        CONF_MODE: user_input[CONF_MODE],
        CONF_TRUSTED_ACK: bool(user_input.get(CONF_TRUSTED_ACK)),
        CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
        CONF_SHOW_IN_SIDEBAR: user_input[CONF_SHOW_IN_SIDEBAR],
    }
    if manual:
        url = parse_http_url(user_input[CONF_URL].strip())
        assert url is not None  # Validated before
        if url.user is not None or url.password is not None:
            # Credentials typed into the URL: keep them where they are used (and redacted)
            username = username or url.user or ""
            password = user_input.get(CONF_PASSWORD) or url.password or password
            url = url.with_user(None)
        options[CONF_URL] = str(url)
    elif (visit := user_input.get(CONF_VISIT_LINK)) and visit != VISIT_DEFAULT:
        options[CONF_VISIT_LINK] = visit
    if username:
        options[CONF_USERNAME] = username
        if password:
            options[CONF_PASSWORD] = password
    if user_input.get(CONF_ICON):
        options[CONF_ICON] = user_input[CONF_ICON]
    return options
