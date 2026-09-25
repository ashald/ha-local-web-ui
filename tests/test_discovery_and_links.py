"""Discovery filter, device registry discovery and device page ("Visit") links."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import (
    ConfigEntryDisabler,
    ConfigEntryState,
    ConfigSubentry,
    ConfigSubentryData,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    async_capture_events,
    async_fire_time_changed,
    mock_integration,
)
from yarl import URL

from custom_components.local_web_ui.const import (
    CONF_DEVICE_ID,
    CONF_DISCOVERY,
    CONF_LINK_DEVICE_PAGES,
    CONF_LINKED_DEVICES,
    CONF_MODE,
    CONF_URL,
    DEVICE_LINK_PREFIX,
    DOMAIN,
    MODE_ISOLATED,
    STORAGE_KEY,
    STORAGE_KEY_JAR,
    SUBENTRY_TYPE_VIEW,
)
from custom_components.local_web_ui.discovery import is_local_ui_url, parse_http_url
from custom_components.local_web_ui.hub import LocalWebUiHub, View

OWNER_DOMAIN = "fake_devices"
PORCH_URL = "http://192.168.1.50/"
KITCHEN_URL = "http://wled-kitchen.local/settings?page=1"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def link_for(view_id: str) -> str:
    return f"homeassistant://local-web-ui/{view_id}"


@pytest.fixture
async def http(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})


@pytest.fixture
def owner(hass: HomeAssistant) -> MockConfigEntry:
    """Config entry of the integration that owns the devices (think ESPHome)."""
    entry = MockConfigEntry(domain=OWNER_DOMAIN, title="Fake devices")
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def registry_events(hass: HomeAssistant) -> list[Event[dict[str, Any]]]:
    return async_capture_events(hass, dr.EVENT_DEVICE_REGISTRY_UPDATED)


def add_device(
    registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    key: str,
    url: str | None,
    name: str | None = None,
    **kwargs: Any,
) -> dr.DeviceEntry:
    return registry.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={(OWNER_DOMAIN, key)},
        name=name or f"Device {key}",
        configuration_url=url,
        **kwargs,
    )


def url_of(registry: dr.DeviceRegistry, device_id: str) -> str | None:
    device = registry.async_get(device_id)
    assert device is not None
    return device.configuration_url


def updates_for(events: list[Event[dict[str, Any]]], device_id: str) -> list[dict[str, Any]]:
    return [e.data for e in events if e.data["device_id"] == device_id]


def view_subentry(url: str, title: str, **data: Any) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={CONF_URL: url, CONF_MODE: MODE_ISOLATED, **data},
        subentry_type=SUBENTRY_TYPE_VIEW,
        title=title,
        unique_id=None,
    )


async def setup_lwu(
    hass: HomeAssistant,
    options: dict[str, Any] | None = None,
    subentries: list[ConfigSubentryData] | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Local Web UIs",
        options=options if options is not None else {},
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def hub_of(entry: MockConfigEntry) -> LocalWebUiHub:
    return entry.runtime_data


async def set_options(hass: HomeAssistant, entry: MockConfigEntry, **options: Any) -> None:
    """Change options the way the options flow does (applied in place, no reload)."""
    hub = hub_of(entry)
    hass.config_entries.async_update_entry(entry, options={**entry.options, **options})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hub_of(entry) is hub


def seed_site_data(hub: LocalWebUiHub, user_id: str, view: View) -> None:
    """Give a user cookies and stored data for a site, as the proxy would."""
    hub.async_store_cookies(user_id, view, ["sid=abc; Path=/"], URL(view.url))
    assert hub.async_apply_storage_write(user_id, view.view_id, "w1", {"k": "v"}, clear=False)


def has_site_data(hub: LocalWebUiHub, user_id: str, view: View) -> bool:
    cookies = [morsel.key for morsel in hub.cookie_jar(user_id, view)]
    storage = hub.shim_storage(user_id, view.view_id)
    writes = hub.applied_writes(user_id, view.view_id)
    assert bool(cookies) == bool(storage) == bool(writes)
    return bool(storage)


def stored_site_keys(hass_storage: dict[str, Any]) -> set[str]:
    data = hass_storage[STORAGE_KEY_JAR]["data"]
    return set(data["cookies"]) | set(data["storage"])


async def flush_storage(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(seconds=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# (1) discovery.parse_http_url / is_local_ui_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://192.168.1.50/", "http://192.168.1.50/"),
        ("https://printer.local:8443/admin/?a=b", "https://printer.local:8443/admin/?a=b"),
        ("HTTP://Router.LAN/", "http://router.lan/"),
        ("http://[fd00::1]:8080/x", "http://[fd00::1]:8080/x"),
        (None, None),
        ("", None),
        ("192.168.1.50", None),  # no scheme
        ("/relative/path", None),
        ("ftp://192.168.1.50/", None),
        ("file:///etc/passwd", None),
        ("javascript:alert(1)", None),
        ("homeassistant://local-web-ui/d_abc", None),
        ("homeassistant://hassio/addon/core_ssh", None),
        ("http://", None),  # no host
        ("http:///path", None),
        ("http://[::1", None),  # unparsable
    ],
)
def test_parse_http_url(value: str | None, expected: str | None) -> None:
    result = parse_http_url(value)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert result == URL(expected)
        assert result.scheme in ("http", "https")
        assert result.host


LOCAL_URLS = [
    # private IPv4 ranges
    "http://192.168.1.50/",
    "http://10.0.0.5:8080/path?x=1",
    "https://172.16.0.1/",
    "http://172.31.255.254/",
    # next to (but outside) Supervisor's 172.30.32.0/23
    "http://172.30.31.255/",
    "http://172.30.34.1/",
    # carrier-grade NAT (Tailscale)
    "http://100.64.0.1/",
    "http://100.127.255.254/",
    # IPv6 ULA
    "http://[fd12:3456:789a::1]/",
    "http://[fc00::1]:8080/",
    # local names
    "http://esp-porch.local/",
    "http://ESP-PORCH.LOCAL./",  # case and trailing dot
    "http://router.lan/",
    "http://nas.home/",
    "http://printer.home.arpa:631/",
    "http://box.internal/",
    "http://nas.localdomain/",
    # single-label host names
    "http://printer/",
    "http://wled-kitchen:8080/",
]

NON_LOCAL_URLS = [
    # public addresses and names
    "http://8.8.8.8/",
    "http://100.63.255.255/",  # just below CGNAT
    "http://100.128.0.1/",  # just above CGNAT
    "http://[2001:4860:4860::8888]/",
    "http://example.com/",
    "https://device.example.org:8443/",
    "http://esp.local.example.com/",
    # loopback
    "http://127.0.0.1:8123/",
    "http://127.1.2.3/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    # link-local
    "http://169.254.1.1/",
    "http://[fe80::1]/",
    "http://[fe80::1%25eth0]/",
    # cloud metadata (the IPv6 one is inside the ULA range)
    "http://169.254.169.254/latest/meta-data/",
    "http://[fd00:ec2::254]/",
    # Supervisor's network (HA OS / Supervised)
    "http://172.30.32.1/",
    "http://172.30.32.2/",
    "http://172.30.33.254:8099/",
    # multicast / unspecified
    "http://224.0.0.251/",
    "http://[ff02::1]/",
    "http://0.0.0.0/",
    "http://[::]/",
    # names of the HA host and its internal services
    "http://localhost/",
    "http://LOCALHOST./",
    "http://localhost:8123/",
    "http://supervisor/",
    "http://homeassistant:8123/",
    "http://hassio/",
    "http://host.docker.internal/",
    # app hostnames on the Supervisor network
    "http://a0d7b954-vscode:8080/",
    "http://core-mosquitto/",
    "http://local-my-app:8099/",
]


@pytest.mark.parametrize("value", LOCAL_URLS)
def test_is_local_ui_url_accepts(value: str) -> None:
    url = parse_http_url(value)
    assert url is not None
    assert is_local_ui_url(url) is True


@pytest.mark.parametrize("value", NON_LOCAL_URLS)
def test_is_local_ui_url_rejects(value: str) -> None:
    url = parse_http_url(value)
    assert url is not None
    assert is_local_ui_url(url) is False


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("http://[::ffff:172.30.32.2]/", id="ipv4-mapped-supervisor"),
        pytest.param("http://[::ffff:172.30.33.5]:8099/", id="ipv4-mapped-supervisor-app"),
        pytest.param("http://localhost.localdomain:8123/", id="localhost-localdomain"),
        pytest.param("http://ip6-localhost:8123/", id="ip6-localhost"),
        pytest.param("http://metadata.google.internal/computeMetadata/v1/", id="gcp-metadata-name"),
    ],
)
def test_is_local_ui_url_rejects_aliases_of_excluded_targets(value: str) -> None:
    """Aliases of loopback, Supervisor's network and cloud metadata are excluded too."""
    url = parse_http_url(value)
    assert url is not None
    assert is_local_ui_url(url) is False


@pytest.mark.parametrize(
    ("value", "own_hosts", "expected"),
    [
        ("http://192.168.1.10:8123/", {("192.168.1.10", 8123)}, False),
        ("http://192.168.1.10:8123/lovelace/0", {("192.168.1.10", 8123)}, False),
        ("http://192.168.1.10:8080/", {("192.168.1.10", 8123)}, True),  # other service
        ("http://192.168.1.11:8123/", {("192.168.1.10", 8123)}, True),
        ("http://homeassistant.local:8123/", {("homeassistant.local", 8123)}, False),
        ("http://HomeAssistant.Local.:8123/", {("homeassistant.local", 8123)}, False),
        # the default port counts as the port
        ("http://ha.lan/", {("ha.lan", 80)}, False),
        ("https://ha.lan/", {("ha.lan", 443)}, False),
        ("https://ha.lan/", {("ha.lan", 80)}, True),
    ],
)
def test_is_local_ui_url_excludes_home_assistant_itself(
    value: str, own_hosts: set[tuple[str, int]], expected: bool
) -> None:
    url = parse_http_url(value)
    assert url is not None
    assert is_local_ui_url(url, frozenset(own_hosts)) is expected


# ---------------------------------------------------------------------------
# (2) discovery from the device registry
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("http")
async def test_discovered_views_from_device_registry(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL, name="Porch light")
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL, name="WLED")
    device_registry.async_update_device(kitchen.id, name_by_user="Kitchen strip")
    add_device(device_registry, owner, "cloud", "https://cloud.example.com/device/1")
    add_device(device_registry, owner, "no-url", None)
    add_device(device_registry, owner, "app", "homeassistant://hassio/addon/core_ssh")
    add_device(device_registry, owner, "loopback", "http://127.0.0.1:8080/")
    add_device(device_registry, owner, "supervisor", "http://172.30.32.2/")
    disabled = add_device(device_registry, owner, "disabled", "http://192.168.1.52/")
    device_registry.async_update_device(disabled.id, disabled_by=dr.DeviceEntryDisabler.USER)

    entry = await setup_lwu(hass, {CONF_LINK_DEVICE_PAGES: False})
    hub = hub_of(entry)

    views = hub.discovered_views()
    assert set(views) == {f"d_{porch.id}", f"d_{kitchen.id}"}

    porch_view = views[f"d_{porch.id}"]
    assert porch_view.name == "Porch light"
    assert porch_view.url == PORCH_URL
    assert str(porch_view.origin) == "http://192.168.1.50"
    assert porch_view.entry == "/"
    assert porch_view.source == "discovered"
    assert porch_view.device_id == porch.id
    assert porch_view.mode == MODE_ISOLATED
    assert porch_view.verify_ssl is False
    assert porch_view.show_in_sidebar is False
    # The owning integration is not loaded, so only the host is shown
    assert porch_view.subtitle == "192.168.1.50"

    kitchen_view = views[f"d_{kitchen.id}"]
    assert kitchen_view.name == "Kitchen strip"  # name_by_user wins
    assert kitchen_view.entry == "/settings?page=1"
    assert kitchen_view.url == KITCHEN_URL
    assert kitchen_view.subtitle == "wled-kitchen.local"

    # get_view and view_for_device agree with the listing
    assert hub.get_view(f"d_{porch.id}") == porch_view
    assert hub.view_for_device(kitchen.id) == kitchen_view
    assert hub.get_view(f"d_{disabled.id}") is None
    assert hub.get_view("d_does-not-exist") is None
    assert hub.get_view(porch.id) is None  # discovered ids carry the prefix


@pytest.mark.usefixtures("http")
async def test_discovered_view_name_and_subtitle_fallbacks(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    mock_integration(hass, MockModule(OWNER_DOMAIN, partial_manifest={"name": "Fake Devices"}))
    unnamed = add_device(device_registry, owner, "unnamed", "http://10.0.0.7:8080/")
    device_registry.async_update_device(unnamed.id, name=None)

    hub = hub_of(await setup_lwu(hass, {CONF_LINK_DEVICE_PAGES: False}))
    view = hub.discovered_views()[f"d_{unnamed.id}"]
    assert view.subtitle == "Fake Devices · 10.0.0.7"
    assert view.name == "Fake Devices · 10.0.0.7"

    # Renames are picked up live
    device_registry.async_update_device(unnamed.id, name="Garage door")
    assert hub.discovered_views()[f"d_{unnamed.id}"].name == "Garage door"
    device_registry.async_update_device(unnamed.id, name_by_user="Garage")
    assert hub.discovered_views()[f"d_{unnamed.id}"].name == "Garage"


@pytest.mark.usefixtures("http")
async def test_discovery_excludes_home_assistant_itself(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    await hass.config.async_update(
        internal_url="http://192.168.1.10:8123", external_url="https://ha.example.com"
    )
    own = add_device(device_registry, owner, "own", "http://192.168.1.10:8123/")
    other_port = add_device(device_registry, owner, "other-port", "http://192.168.1.10:8080/")

    hub = hub_of(await setup_lwu(hass))
    assert set(hub.discovered_views()) == {f"d_{other_port.id}"}
    # Not linked either
    assert url_of(device_registry, own.id) == "http://192.168.1.10:8123/"
    assert url_of(device_registry, other_port.id) == link_for(f"d_{other_port.id}")


@pytest.mark.usefixtures("http")
async def test_discovery_option_off(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    registry_events.clear()

    hub = hub_of(await setup_lwu(hass, {CONF_DISCOVERY: False}))

    assert hub.discovered_views() == {}
    assert hub.discovered_views(include_hidden=True) == {}
    assert hub.get_view(f"d_{porch.id}") is None
    assert hub.view_for_device(porch.id) is None
    # Nothing to link to, so the device page is left alone
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert registry_events == []


@pytest.mark.usefixtures("http")
async def test_discovery_option_turned_off_later_restores_links(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    entry = await setup_lwu(hass)
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")

    registry_events.clear()
    await set_options(hass, entry, **{CONF_DISCOVERY: False})
    assert hub_of(entry).discovered_views() == {}
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert len(updates_for(registry_events, porch.id)) == 1

    registry_events.clear()
    await set_options(hass, entry, **{CONF_DISCOVERY: True})
    assert set(hub_of(entry).discovered_views()) == {f"d_{porch.id}"}
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert len(updates_for(registry_events, porch.id)) == 1


@pytest.mark.usefixtures("http")
async def test_static_view_pinned_to_device_replaces_discovered(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL, name="Porch light")
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    entry = await setup_lwu(
        hass,
        subentries=[
            view_subentry("http://192.168.1.50/admin", "Porch admin", **{CONF_DEVICE_ID: porch.id}),
            view_subentry("http://nas.lan:5000/", "NAS"),
        ],
    )
    hub = hub_of(entry)
    static_ids = {s.title: s.subentry_id for s in entry.subentries.values()}
    pinned_id = static_ids["Porch admin"]

    # Only the unpinned device is discovered; the pinned one is served by its static view
    assert set(hub.discovered_views(include_hidden=True)) == {f"d_{kitchen.id}"}
    assert hub.get_view(f"d_{porch.id}") is None
    pinned = hub.view_for_device(porch.id)
    assert pinned is not None
    assert pinned.view_id == pinned_id
    assert pinned.source == "static"
    assert pinned.name == "Porch admin"
    assert pinned.url == "http://192.168.1.50/admin"
    assert set(hub.static_views) == set(static_ids.values())

    # The device page links to the static view
    assert url_of(device_registry, porch.id) == link_for(pinned_id)
    assert hub.originals[porch.id] == PORCH_URL
    assert url_of(device_registry, kitchen.id) == link_for(f"d_{kitchen.id}")


@pytest.mark.usefixtures("http")
async def test_pinning_and_unpinning_relinks_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    entry = await setup_lwu(hass)
    hub = hub_of(entry)
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")

    # Pin: a subentry linked to the device is added and applied in place
    registry_events.clear()
    pinned = ConfigSubentry(**view_subentry(PORCH_URL, "Porch", **{CONF_DEVICE_ID: porch.id}))
    hass.config_entries.async_add_subentry(entry, pinned)
    await hass.async_block_till_done()
    assert hub_of(entry) is hub
    assert url_of(device_registry, porch.id) == link_for(pinned.subentry_id)
    assert hub.discovered_views(include_hidden=True) == {}
    assert len(updates_for(registry_events, porch.id)) == 1

    # Unpin: back to the discovered view, same original
    registry_events.clear()
    hass.config_entries.async_remove_subentry(entry, pinned.subentry_id)
    await hass.async_block_till_done()
    assert hub_of(entry) is hub
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert hub.originals[porch.id] == PORCH_URL
    assert len(updates_for(registry_events, porch.id)) == 1


@pytest.mark.usefixtures("http")
async def test_hidden_views(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    entry = await setup_lwu(hass, {CONF_LINK_DEVICE_PAGES: False})
    hub = hub_of(entry)

    hub.async_set_hidden(f"d_{porch.id}", True)
    assert set(hub.discovered_views()) == {f"d_{kitchen.id}"}
    assert set(hub.discovered_views(include_hidden=True)) == {
        f"d_{porch.id}",
        f"d_{kitchen.id}",
    }

    # Hidden state is persisted and survives a reload
    await flush_storage(hass, freezer)
    assert hass_storage[STORAGE_KEY]["data"]["hidden"] == [f"d_{porch.id}"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    hub = hub_of(entry)
    assert set(hub.discovered_views()) == {f"d_{kitchen.id}"}

    hub.async_set_hidden(f"d_{porch.id}", False)
    assert set(hub.discovered_views()) == {f"d_{porch.id}", f"d_{kitchen.id}"}


# ---------------------------------------------------------------------------
# (3) device page links
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("http")
async def test_device_pages_linked_on_setup(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    cloud = add_device(device_registry, owner, "cloud", "https://cloud.example.com/d/1")
    no_url = add_device(device_registry, owner, "no-url", None)
    registry_events.clear()

    entry = await setup_lwu(hass)
    hub = hub_of(entry)

    assert DEVICE_LINK_PREFIX == "homeassistant://local-web-ui/"
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert url_of(device_registry, kitchen.id) == link_for(f"d_{kitchen.id}")
    assert url_of(device_registry, cloud.id) == "https://cloud.example.com/d/1"
    assert url_of(device_registry, no_url.id) is None
    assert hub.originals[porch.id] == PORCH_URL
    assert hub.originals[kitchen.id] == KITCHEN_URL
    assert no_url.id not in hub.originals

    # One registry update per linked device, nothing else
    assert len(updates_for(registry_events, porch.id)) == 1
    assert len(updates_for(registry_events, kitchen.id)) == 1
    assert updates_for(registry_events, cloud.id) == []
    assert updates_for(registry_events, no_url.id) == []
    assert updates_for(registry_events, porch.id)[0]["changes"] == {"configuration_url": PORCH_URL}

    # The view still targets the device's own URL, looking through the link
    assert hub.get_view(f"d_{porch.id}").url == PORCH_URL
    assert hub.device_ui_url(device_registry.async_get(porch.id)) == URL(PORCH_URL)

    # Originals are persisted
    await flush_storage(hass, freezer)
    stored = hass_storage[STORAGE_KEY]["data"]["originals"]
    assert stored[porch.id] == PORCH_URL
    assert stored[kitchen.id] == KITCHEN_URL


@pytest.mark.usefixtures("http")
async def test_device_created_after_setup_is_linked(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    hub = hub_of(await setup_lwu(hass))
    registry_events.clear()

    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert hub.originals[porch.id] == PORCH_URL
    actions = [e["action"] for e in updates_for(registry_events, porch.id)]
    assert actions == ["create", "update"]


@pytest.mark.usefixtures("http")
async def test_reload_is_stable(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    entry = await setup_lwu(hass)
    registry_events.clear()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # Originals come back from storage and nothing is rewritten
    assert registry_events == []
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert hub_of(entry).originals[porch.id] == PORCH_URL
    assert hub_of(entry).get_view(f"d_{porch.id}").url == PORCH_URL


@pytest.mark.usefixtures("http")
async def test_owning_integration_updates_url(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    hub = hub_of(await setup_lwu(hass))
    link = link_for(f"d_{porch.id}")

    # The integration moves the device (new IP and web_server port)
    registry_events.clear()
    new_url = "http://192.168.1.77:8080/"
    device_registry.async_update_device(porch.id, configuration_url=new_url)
    assert hub.originals[porch.id] == new_url
    assert url_of(device_registry, porch.id) == link
    assert hub.get_view(f"d_{porch.id}").url == new_url
    # Theirs and ours; no ping-pong
    assert [e["changes"] for e in updates_for(registry_events, porch.id)] == [
        {"configuration_url": link},
        {"configuration_url": new_url},
    ]

    # The integration re-registers the device with the same URL (e.g. reconnect)
    registry_events.clear()
    add_device(device_registry, owner, "porch", new_url)
    assert url_of(device_registry, porch.id) == link
    assert hub.originals[porch.id] == new_url
    assert len(updates_for(registry_events, porch.id)) == 2

    # Unrelated changes do not touch the link
    registry_events.clear()
    device_registry.async_update_device(porch.id, name_by_user="Porch", sw_version="2026.9.0")
    assert url_of(device_registry, porch.id) == link
    assert len(updates_for(registry_events, porch.id)) == 1


@pytest.mark.usefixtures("http")
async def test_owning_integration_moves_url_out_of_scope(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    hub = hub_of(await setup_lwu(hass))

    # A non-local URL is left in place and the view goes away
    registry_events.clear()
    device_registry.async_update_device(porch.id, configuration_url="https://cloud.example.com/")
    assert url_of(device_registry, porch.id) == "https://cloud.example.com/"
    assert hub.get_view(f"d_{porch.id}") is None
    assert len(updates_for(registry_events, porch.id)) == 1

    # Removing the URL forgets the original
    registry_events.clear()
    device_registry.async_update_device(kitchen.id, configuration_url=None)
    assert url_of(device_registry, kitchen.id) is None
    assert kitchen.id not in hub.originals
    assert hub.get_view(f"d_{kitchen.id}") is None
    assert len(updates_for(registry_events, kitchen.id)) == 1

    # And a local URL coming back is linked again
    registry_events.clear()
    device_registry.async_update_device(kitchen.id, configuration_url=KITCHEN_URL)
    assert url_of(device_registry, kitchen.id) == link_for(f"d_{kitchen.id}")
    assert hub.originals[kitchen.id] == KITCHEN_URL
    assert len(updates_for(registry_events, kitchen.id)) == 2


@pytest.mark.usefixtures("http")
async def test_per_device_override(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    entry = await setup_lwu(hass)
    hub = hub_of(entry)

    registry_events.clear()
    hub.async_set_device_link(porch.id, False)
    assert hub.link_enabled(porch.id) is False
    assert hub.link_overrides == {porch.id: False}
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert url_of(device_registry, kitchen.id) == link_for(f"d_{kitchen.id}")
    assert len(updates_for(registry_events, porch.id)) == 1
    assert updates_for(registry_events, kitchen.id) == []
    # The view itself is still there, but the original is only kept while linked
    assert hub.get_view(f"d_{porch.id}") is not None
    assert porch.id not in hub.originals

    # Setting it again is a no-op
    registry_events.clear()
    hub.async_set_device_link(porch.id, False)
    assert registry_events == []

    # URL changes by the integration are left alone and not linked
    registry_events.clear()
    new_url = "http://192.168.1.78/"
    device_registry.async_update_device(porch.id, configuration_url=new_url)
    assert url_of(device_registry, porch.id) == new_url
    assert porch.id not in hub.originals
    assert hub.get_view(f"d_{porch.id}").url == new_url
    assert len(updates_for(registry_events, porch.id)) == 1

    # The override survives a reload
    await flush_storage(hass, freezer)
    assert hass_storage[STORAGE_KEY]["data"]["link_overrides"] == {porch.id: False}
    assert porch.id not in hass_storage[STORAGE_KEY]["data"]["originals"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    hub = hub_of(entry)
    assert hub.link_overrides == {porch.id: False}
    assert url_of(device_registry, porch.id) == new_url

    # Back to the default (None): linked again, to the current URL
    registry_events.clear()
    hub.async_set_device_link(porch.id, None)
    assert hub.link_overrides == {}
    assert hub.link_enabled(porch.id) is True
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert hub.originals[porch.id] == new_url
    assert hub.get_view(f"d_{porch.id}").url == new_url
    assert len(updates_for(registry_events, porch.id)) == 1

    # An explicit "on" is stored as such, and changes nothing here
    registry_events.clear()
    hub.async_set_device_link(porch.id, True)
    assert hub.link_overrides == {porch.id: True}
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert registry_events == []


@pytest.mark.usefixtures("http")
async def test_per_device_opt_in_with_global_option_off(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    registry_events.clear()
    hub = hub_of(await setup_lwu(hass, {CONF_LINK_DEVICE_PAGES: False}))
    assert registry_events == []
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert hub.originals == {}

    hub.async_set_device_link(porch.id, True)
    assert hub.link_enabled(porch.id) is True
    assert hub.link_overrides == {porch.id: True}
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert url_of(device_registry, kitchen.id) == KITCHEN_URL
    assert hub.originals == {porch.id: PORCH_URL}

    # Overrides are absolute: an explicit "off" is kept even though it matches the default
    hub.async_set_device_link(porch.id, False)
    assert hub.link_overrides == {porch.id: False}
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert hub.originals == {}

    # None follows the global option again (off)
    hub.async_set_device_link(porch.id, True)
    hub.async_set_device_link(porch.id, None)
    assert hub.link_overrides == {}
    assert hub.link_enabled(porch.id) is False
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert hub.originals == {}


@pytest.mark.usefixtures("http")
async def test_global_option_off_restores_all(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    entry = await setup_lwu(hass)
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")

    registry_events.clear()
    await set_options(hass, entry, **{CONF_LINK_DEVICE_PAGES: False})
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert url_of(device_registry, kitchen.id) == KITCHEN_URL
    assert len(updates_for(registry_events, porch.id)) == 1
    assert len(updates_for(registry_events, kitchen.id)) == 1
    assert hub_of(entry).originals == {}
    # Views are still discovered
    assert set(hub_of(entry).discovered_views()) == {f"d_{porch.id}", f"d_{kitchen.id}"}

    # Later URL changes are not linked either
    registry_events.clear()
    device_registry.async_update_device(porch.id, configuration_url="http://192.168.1.79/")
    assert url_of(device_registry, porch.id) == "http://192.168.1.79/"
    assert len(updates_for(registry_events, porch.id)) == 1

    registry_events.clear()
    await set_options(hass, entry, **{CONF_LINK_DEVICE_PAGES: True})
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert url_of(device_registry, kitchen.id) == link_for(f"d_{kitchen.id}")
    assert hub_of(entry).originals[porch.id] == "http://192.168.1.79/"
    assert len(updates_for(registry_events, porch.id)) == 1


@pytest.mark.usefixtures("http")
async def test_global_option_off_keeps_explicit_opt_out(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    entry = await setup_lwu(hass)
    hub = hub_of(entry)
    hub.async_set_device_link(porch.id, False)  # "don't link this one"
    assert url_of(device_registry, porch.id) == PORCH_URL

    await set_options(hass, entry, **{CONF_LINK_DEVICE_PAGES: False})  # "don't link any"

    assert url_of(device_registry, kitchen.id) == KITCHEN_URL
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert hub.originals == {}

    # "Link all" again still leaves out the device the user opted out
    await set_options(hass, entry, **{CONF_LINK_DEVICE_PAGES: True})
    assert url_of(device_registry, kitchen.id) == link_for(f"d_{kitchen.id}")
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert hub.originals == {kitchen.id: KITCHEN_URL}


@pytest.mark.usefixtures("http")
async def test_hiding_view_restores_device_link(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    hub = hub_of(await setup_lwu(hass))

    registry_events.clear()
    hub.async_set_hidden(f"d_{porch.id}", True)
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert url_of(device_registry, kitchen.id) == link_for(f"d_{kitchen.id}")
    assert len(updates_for(registry_events, porch.id)) == 1
    assert porch.id not in hub.originals

    # While hidden, URL changes are not linked
    registry_events.clear()
    device_registry.async_update_device(porch.id, configuration_url="http://192.168.1.80/")
    assert url_of(device_registry, porch.id) == "http://192.168.1.80/"
    assert porch.id not in hub.originals
    assert len(updates_for(registry_events, porch.id)) == 1

    registry_events.clear()
    hub.async_set_hidden(f"d_{porch.id}", False)
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert hub.originals[porch.id] == "http://192.168.1.80/"
    assert len(updates_for(registry_events, porch.id)) == 1


@pytest.mark.usefixtures("http")
async def test_disabling_device_restores_link(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    hub = hub_of(await setup_lwu(hass))

    registry_events.clear()
    device_registry.async_update_device(porch.id, disabled_by=dr.DeviceEntryDisabler.USER)
    assert hub.discovered_views() == {}
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert porch.id not in hub.originals
    assert len(updates_for(registry_events, porch.id)) == 2  # disable + restore

    registry_events.clear()
    device_registry.async_update_device(porch.id, disabled_by=None)
    assert set(hub.discovered_views()) == {f"d_{porch.id}"}
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert len(updates_for(registry_events, porch.id)) == 2  # enable + link


@pytest.mark.usefixtures("http")
async def test_remove_entry_restores_originals_and_storage(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    cloud = add_device(device_registry, owner, "cloud", "https://cloud.example.com/d/1")
    entry = await setup_lwu(hass)
    hub = hub_of(entry)
    hub.async_set_hidden("d_something", True)  # make sure there is something stored
    await flush_storage(hass, freezer)
    assert STORAGE_KEY in hass_storage
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert url_of(device_registry, porch.id) == PORCH_URL
    assert url_of(device_registry, kitchen.id) == KITCHEN_URL
    assert url_of(device_registry, cloud.id) == "https://cloud.example.com/d/1"
    assert STORAGE_KEY not in hass_storage
    assert STORAGE_KEY_JAR not in hass_storage

    # Nothing listens any more, and no delayed save brings the storage back
    device_registry.async_update_device(porch.id, configuration_url="http://192.168.1.81/")
    await flush_storage(hass, freezer)
    assert url_of(device_registry, porch.id) == "http://192.168.1.81/"
    assert STORAGE_KEY not in hass_storage
    assert STORAGE_KEY_JAR not in hass_storage


@pytest.mark.usefixtures("http")
async def test_remove_entry_restores_url_changed_just_before(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    """An original that was refreshed but not saved yet is what gets restored."""
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    entry = await setup_lwu(hass)
    device_registry.async_update_device(porch.id, configuration_url="http://192.168.1.82/")
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert url_of(device_registry, porch.id) == "http://192.168.1.82/"


@pytest.mark.usefixtures("http")
async def test_disable_entry_restores_originals(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    entry = await setup_lwu(hass)

    registry_events.clear()
    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert url_of(device_registry, kitchen.id) == KITCHEN_URL
    assert len(updates_for(registry_events, porch.id)) == 1

    # While disabled, nothing reacts to URL changes
    registry_events.clear()
    device_registry.async_update_device(porch.id, configuration_url="http://192.168.1.83/")
    assert url_of(device_registry, porch.id) == "http://192.168.1.83/"
    assert len(updates_for(registry_events, porch.id)) == 1

    # Re-enabling links them again, using the current URL as the original
    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert url_of(device_registry, kitchen.id) == link_for(f"d_{kitchen.id}")
    assert hub_of(entry).originals[porch.id] == "http://192.168.1.83/"


@pytest.mark.usefixtures("http")
async def test_device_removal_forgets_original(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    hub = hub_of(await setup_lwu(hass))
    await flush_storage(hass, freezer)
    assert porch.id in hass_storage[STORAGE_KEY]["data"]["originals"]

    device_registry.async_remove_device(porch.id)
    assert porch.id not in hub.originals
    assert kitchen.id in hub.originals
    assert hub.get_view(f"d_{porch.id}") is None

    await flush_storage(hass, freezer)
    stored = hass_storage[STORAGE_KEY]["data"]["originals"]
    assert porch.id not in stored
    assert stored[kitchen.id] == KITCHEN_URL


@pytest.mark.usefixtures("http")
async def test_registry_updates_are_bounded(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    """A whole session of changes never makes us chase our own updates."""
    devices = [
        add_device(device_registry, owner, f"esp{i}", f"http://192.168.2.{i + 10}/")
        for i in range(5)
    ]
    registry_events.clear()
    entry = await setup_lwu(hass)
    assert len(registry_events) == len(devices)

    hub = hub_of(entry)
    registry_events.clear()
    for i, device in enumerate(devices):
        # Each its own URL: devices sharing one would share a single view
        device_registry.async_update_device(device.id, configuration_url=f"http://10.1.1.{i}/")
        hub.async_set_device_link(device.id, False)
        hub.async_set_device_link(device.id, True)
        hub.async_set_hidden(f"d_{device.id}", True)
        hub.async_set_hidden(f"d_{device.id}", False)
        device_registry.async_update_device(device.id, name_by_user="renamed")
    await hass.async_block_till_done()
    # per device: theirs + relink, unlink, link, unlink, link, rename
    assert len(registry_events) == 7 * len(devices)
    for device in devices:
        assert url_of(device_registry, device.id) == link_for(f"d_{device.id}")


@pytest.mark.usefixtures("http")
async def test_child_devices_are_ignored(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Child devices have no web UI of their own; nothing may read their missing fields."""
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    entry = await setup_lwu(hass, {CONF_LINKED_DEVICES: True})
    hub = hub_of(entry)

    registry_events.clear()
    child = device_registry.async_get_or_create_child(
        config_entry_id=owner.entry_id,
        identifiers={(OWNER_DOMAIN, "porch-relay")},
        name="Relay",
        parent_device_id=porch.id,
    )
    device_registry.async_update_child_device(child.id, name_by_user="Porch relay")
    device_registry.async_update_child_device(child.id, disabled_by=dr.DeviceEntryDisabler.USER)
    hub.async_set_device_link(child.id, True)
    assert hub.get_view(f"d_{child.id}") is None
    assert hub.view_for_device(child.id) is None
    assert hub.area_name(child.id) is None
    assert child.id not in hub.originals
    assert updates_for(registry_events, porch.id) == []

    # A full re-sync walks the registry without touching child devices
    await set_options(hass, entry, **{CONF_LINK_DEVICE_PAGES: False})
    await set_options(hass, entry, **{CONF_LINK_DEVICE_PAGES: True})
    device_registry.async_remove_device(child.id)
    await hass.async_block_till_done()

    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert set(hub.discovered_views()) == {f"d_{porch.id}"}
    assert [d.name for d in dr.async_entries_for_config_entry(device_registry, entry.entry_id)] == [
        "Device porch web UI"
    ]
    assert "ChildDeviceEntry" not in caplog.text
    assert "Detected that custom integration" not in caplog.text


# ---------------------------------------------------------------------------
# (4) one view per URL
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("http")
async def test_views_are_deduplicated_by_url(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    """A device known to two integrations is two devices with the same web UI."""
    other_owner = MockConfigEntry(domain="other_devices", title="Other devices")
    other_owner.add_to_hass(hass)
    first = add_device(device_registry, owner, "porch", PORCH_URL, name="Porch (fake)")
    second = device_registry.async_get_or_create(
        config_entry_id=other_owner.entry_id,
        identifiers={("other_devices", "porch")},
        name="Porch (other)",
        configuration_url="http://192.168.1.50",  # same URL, spelled differently
    )
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    hub = hub_of(await setup_lwu(hass))

    kept_id = f"d_{first.id}"
    assert set(hub.discovered_views(include_hidden=True)) == {kept_id, f"d_{kitchen.id}"}
    assert hub.get_view(f"d_{second.id}") is None
    assert hub.view_for_device(first.id) == hub.get_view(kept_id)
    assert hub.view_for_device(second.id) == hub.get_view(kept_id)
    assert hub.get_view(kept_id).name == "Porch (fake)"  # the first device's

    # Both device pages open the one view; each keeps its own original
    assert url_of(device_registry, first.id) == link_for(kept_id)
    assert url_of(device_registry, second.id) == link_for(kept_id)
    assert hub.originals == {
        first.id: PORCH_URL,
        second.id: "http://192.168.1.50",
        kitchen.id: KITCHEN_URL,
    }

    # The other device's page link can still be turned off on its own
    hub.async_set_device_link(second.id, False)
    assert url_of(device_registry, second.id) == "http://192.168.1.50"
    assert url_of(device_registry, first.id) == link_for(kept_id)
    assert hub.get_view(kept_id) is not None


@pytest.mark.usefixtures("http")
async def test_static_view_with_same_url_wins_over_discovery(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    entry = await setup_lwu(hass, subentries=[view_subentry(PORCH_URL, "Porch by hand")])
    hub = hub_of(entry)
    [static_id] = hub.static_views

    assert set(hub.discovered_views(include_hidden=True)) == {f"d_{kitchen.id}"}
    assert hub.get_view(f"d_{porch.id}") is None
    view = hub.view_for_device(porch.id)
    assert view is not None
    assert view.view_id == static_id
    assert view.source == "static"
    assert url_of(device_registry, porch.id) == link_for(static_id)
    assert hub.originals[porch.id] == PORCH_URL

    # Removing the static view gives the device its discovered view back
    hass.config_entries.async_remove_subentry(entry, static_id)
    await hass.async_block_till_done()
    assert hub_of(entry) is hub
    assert hub.view_for_device(porch.id).view_id == f"d_{porch.id}"
    assert url_of(device_registry, porch.id) == link_for(f"d_{porch.id}")
    assert hub.originals[porch.id] == PORCH_URL

    # And adding one with the same URL takes it over again, in place
    added = ConfigSubentry(**view_subentry(PORCH_URL, "Porch again"))
    hass.config_entries.async_add_subentry(entry, added)
    await hass.async_block_till_done()
    assert hub.get_view(f"d_{porch.id}") is None
    assert url_of(device_registry, porch.id) == link_for(added.subentry_id)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"remove": True}, id="removed"),
        pytest.param({"configuration_url": "http://192.168.1.60/"}, id="url-changed"),
        pytest.param({"disabled_by": dr.DeviceEntryDisabler.USER}, id="disabled"),
    ],
)
@pytest.mark.usefixtures("http")
async def test_duplicate_takes_over_when_kept_device_changes(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    change: dict[str, Any],
) -> None:
    first = add_device(device_registry, owner, "first", PORCH_URL)
    second = add_device(device_registry, owner, "second", PORCH_URL)
    hub = hub_of(await setup_lwu(hass))
    assert url_of(device_registry, second.id) == link_for(f"d_{first.id}")

    if change.pop("remove", False):
        device_registry.async_remove_device(first.id)
    else:
        device_registry.async_update_device(first.id, **change)
    await hass.async_block_till_done()

    # The second device now owns the URL's view, and its page opens it
    assert hub.view_for_device(second.id).view_id == f"d_{second.id}"
    assert url_of(device_registry, second.id) == link_for(f"d_{second.id}")
    assert hub.originals[second.id] == PORCH_URL


@pytest.mark.usefixtures("http")
async def test_hiding_view_resyncs_every_device_mapped_to_it(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    first = add_device(device_registry, owner, "first", PORCH_URL)
    second = add_device(device_registry, owner, "second", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    hub = hub_of(await setup_lwu(hass))
    kept_id = f"d_{first.id}"

    registry_events.clear()
    hub.async_set_hidden(kept_id, True)
    assert url_of(device_registry, first.id) == PORCH_URL
    assert url_of(device_registry, second.id) == PORCH_URL
    assert url_of(device_registry, kitchen.id) == link_for(f"d_{kitchen.id}")
    assert hub.originals == {kitchen.id: KITCHEN_URL}
    assert len(updates_for(registry_events, first.id)) == 1
    assert len(updates_for(registry_events, second.id)) == 1
    assert updates_for(registry_events, kitchen.id) == []
    # Hiding does not hand the URL to the other device
    assert hub.get_view(f"d_{second.id}") is None

    registry_events.clear()
    hub.async_set_hidden(kept_id, False)
    assert url_of(device_registry, first.id) == link_for(kept_id)
    assert url_of(device_registry, second.id) == link_for(kept_id)
    assert hub.originals == {first.id: PORCH_URL, second.id: PORCH_URL, kitchen.id: KITCHEN_URL}
    assert len(registry_events) == 2


# ---------------------------------------------------------------------------
# (5) originals and our own devices
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("http")
async def test_originals_only_kept_while_linked(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    cloud = add_device(device_registry, owner, "cloud", "https://cloud.example.com/d/1")
    entry = await setup_lwu(hass)
    hub = hub_of(entry)
    assert hub.originals == {porch.id: PORCH_URL, kitchen.id: KITCHEN_URL}
    assert cloud.id not in hub.originals

    async def stored() -> dict[str, str]:
        await flush_storage(hass, freezer)
        return hass_storage[STORAGE_KEY]["data"]["originals"]

    assert await stored() == {porch.id: PORCH_URL, kitchen.id: KITCHEN_URL}

    # Each way of no longer linking a device gives the URL back and drops the original
    hub.async_set_device_link(porch.id, False)
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert hub.originals == {kitchen.id: KITCHEN_URL}
    assert await stored() == {kitchen.id: KITCHEN_URL}

    hub.async_set_hidden(f"d_{kitchen.id}", True)
    assert url_of(device_registry, kitchen.id) == KITCHEN_URL
    assert hub.originals == {}
    assert await stored() == {}

    hub.async_set_device_link(porch.id, None)
    hub.async_set_hidden(f"d_{kitchen.id}", False)
    assert hub.originals == {porch.id: PORCH_URL, kitchen.id: KITCHEN_URL}

    await set_options(hass, entry, **{CONF_DISCOVERY: False})
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert url_of(device_registry, kitchen.id) == KITCHEN_URL
    assert hub.originals == {}
    assert await stored() == {}

    await set_options(hass, entry, **{CONF_DISCOVERY: True})
    assert hub.originals == {porch.id: PORCH_URL, kitchen.id: KITCHEN_URL}

    # The integration moving the device out of scope drops it too
    device_registry.async_update_device(porch.id, configuration_url="https://cloud.example.com/")
    assert hub.originals == {kitchen.id: KITCHEN_URL}
    assert await stored() == {kitchen.id: KITCHEN_URL}


@pytest.mark.usefixtures("http")
async def test_own_linked_devices_are_never_visit_linked(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    entry = await setup_lwu(hass, {CONF_LINKED_DEVICES: True, CONF_LINK_DEVICE_PAGES: True})
    hub = hub_of(entry)
    view_id = f"d_{porch.id}"

    [linked] = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert linked.configuration_url == link_for(view_id)
    assert url_of(device_registry, porch.id) == link_for(view_id)
    assert set(hub.originals) == {porch.id}
    assert hub.view_for_device(linked.id) is None

    # A full re-sync, a per-device choice and a user rename leave it alone
    registry_events.clear()
    await set_options(hass, entry, **{CONF_LINK_DEVICE_PAGES: False})
    await set_options(hass, entry, **{CONF_LINK_DEVICE_PAGES: True})
    hub.async_set_device_link(linked.id, False)
    hub.async_set_device_link(linked.id, True)
    device_registry.async_update_device(linked.id, name_by_user="My porch page")
    await hass.async_block_till_done()
    assert [e["changes"] for e in updates_for(registry_events, linked.id)] == [
        {"name_by_user": None}
    ]
    assert url_of(device_registry, linked.id) == link_for(view_id)
    assert linked.id not in hub.originals

    # Even with a web URL of its own, a device of our entry is not linked
    own = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("elsewhere", "own")},
        configuration_url="http://192.168.1.99/",
    )
    assert url_of(device_registry, own.id) == "http://192.168.1.99/"
    assert own.id not in hub.originals


# ---------------------------------------------------------------------------
# (6) per-user site data of web UIs that go away
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("http")
async def test_device_removal_forgets_site_data(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    hub = hub_of(await setup_lwu(hass))
    porch_view = hub.get_view(f"d_{porch.id}")
    kitchen_view = hub.get_view(f"d_{kitchen.id}")
    for user_id in ("alice", "bob"):
        seed_site_data(hub, user_id, porch_view)
        seed_site_data(hub, user_id, kitchen_view)
    await flush_storage(hass, freezer)
    assert stored_site_keys(hass_storage) == {
        f"{user}|{view.view_id}" for user in ("alice", "bob") for view in (porch_view, kitchen_view)
    }

    device_registry.async_remove_device(porch.id)
    for user_id in ("alice", "bob"):
        assert not has_site_data(hub, user_id, porch_view)
        assert has_site_data(hub, user_id, kitchen_view)
    await flush_storage(hass, freezer)
    assert stored_site_keys(hass_storage) == {
        f"{user}|{kitchen_view.view_id}" for user in ("alice", "bob")
    }


@pytest.mark.usefixtures("http")
async def test_user_removal_forgets_sessions_and_site_data(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    hub = hub_of(await setup_lwu(hass))
    view = hub.get_view(f"d_{porch.id}")
    leaving = await hass.auth.async_create_user("Leaving")
    staying = await hass.auth.async_create_user("Staying")
    sessions = {
        user.id: hub.sessions.create(view.view_id, user.id, None) for user in (leaving, staying)
    }
    for user in (leaving, staying):
        seed_site_data(hub, user.id, view)

    await hass.auth.async_remove_user(leaving)
    await hass.async_block_till_done()

    assert hub.sessions.get(sessions[leaving.id].token) is None
    assert hub.sessions.touch(sessions[leaving.id].token, view.view_id) is None
    assert hub.sessions.touch(sessions[staying.id].token, view.view_id) is not None
    assert not has_site_data(hub, leaving.id, view)
    assert has_site_data(hub, staying.id, view)
    await flush_storage(hass, freezer)
    assert stored_site_keys(hass_storage) == {f"{staying.id}|{view.view_id}"}


@pytest.mark.usefixtures("http")
async def test_removing_static_view_forgets_site_data(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    entry = await setup_lwu(
        hass,
        subentries=[
            view_subentry("http://nas.lan:5000/", "NAS"),
            view_subentry("http://printer.lan/", "Printer"),
        ],
    )
    hub = hub_of(entry)
    views = {view.name: view for view in hub.static_views.values()}
    for user_id in ("alice", "bob"):
        for view in views.values():
            seed_site_data(hub, user_id, view)

    hass.config_entries.async_remove_subentry(entry, views["NAS"].view_id)
    await hass.async_block_till_done()

    assert hub_of(entry) is hub
    assert hub.get_view(views["NAS"].view_id) is None
    for user_id in ("alice", "bob"):
        assert not has_site_data(hub, user_id, views["NAS"])
        assert has_site_data(hub, user_id, views["Printer"])
    await flush_storage(hass, freezer)
    assert stored_site_keys(hass_storage) == {
        f"{user_id}|{views['Printer'].view_id}" for user_id in ("alice", "bob")
    }


# ---------------------------------------------------------------------------
# (7) storage
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("http")
async def test_disable_entry_restores_unsaved_originals(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Links are restored from the originals in memory, not from what was saved."""
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    entry = await setup_lwu(hass)
    await flush_storage(hass, freezer)
    assert hass_storage[STORAGE_KEY]["data"]["originals"] == {porch.id: PORCH_URL}

    # Changed right before disabling: the delayed save has not run yet
    device_registry.async_update_device(porch.id, configuration_url="http://192.168.1.84/")
    assert hass_storage[STORAGE_KEY]["data"]["originals"] == {porch.id: PORCH_URL}

    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert url_of(device_registry, porch.id) == "http://192.168.1.84/"


@pytest.mark.usefixtures("http")
async def test_stores_are_private(hass: HomeAssistant) -> None:
    """Stored cookies and site storage hold device credentials: owner-only files."""
    with patch("custom_components.local_web_ui.hub.Store", wraps=Store) as store_cls:
        await setup_lwu(hass)
    keys = set()
    for call in store_cls.call_args_list:
        keys.add(call.args[2])
        assert call.kwargs.get("private") is True, call
        assert call.kwargs.get("atomic_writes") is True, call
    assert keys == {STORAGE_KEY, STORAGE_KEY_JAR}
