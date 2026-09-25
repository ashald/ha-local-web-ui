"""Discovery filter, discovered web UI offers and device page ("Visit") links."""

from __future__ import annotations

from datetime import timedelta
import ipaddress
import socket
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_INTEGRATION_DISCOVERY,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
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
    CONF_KIND,
    CONF_LINK_DEVICE_PAGES,
    CONF_MODE,
    CONF_SHOW_IN_SIDEBAR,
    CONF_SHOW_PANEL,
    CONF_TRUSTED_ACK,
    CONF_URL,
    CONF_VERIFY_SSL,
    CONF_VISIT_LINK,
    DOMAIN,
    HUB_UNIQUE_ID,
    KIND_HUB,
    KIND_VIEW,
    MODE_ISOLATED,
    STORAGE_KEY,
    STORAGE_KEY_JAR,
    VISIT_DEVICE,
    VISIT_HERE,
)
from custom_components.local_web_ui.discovery import (
    LanOnlyResolver,
    is_lan_address,
    is_local_ui_url,
    parse_http_url,
)
from custom_components.local_web_ui.hub import LocalWebUiHub, View, device_unique_id

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


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def setup_hub(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    """The hub entry, with the defaults of the config flow unless given."""
    return await _setup(
        hass,
        MockConfigEntry(
            domain=DOMAIN,
            version=2,
            title="Local Web UIs",
            unique_id=HUB_UNIQUE_ID,
            data={CONF_KIND: KIND_HUB},
            options={
                CONF_DISCOVERY: True,
                CONF_LINK_DEVICE_PAGES: True,
                CONF_SHOW_PANEL: True,
                **options,
            },
        ),
    )


async def add_device_web_ui(
    hass: HomeAssistant, device: dr.DeviceEntry | Any, title: str | None = None, **options: Any
) -> MockConfigEntry:
    """A web UI entry for a device, as accepting its discovery creates it."""
    entry = await _setup(
        hass,
        MockConfigEntry(
            domain=DOMAIN,
            version=2,
            title=title or device.name or "Device",
            unique_id=device_unique_id(device.id),
            source=SOURCE_INTEGRATION_DISCOVERY,
            data={CONF_KIND: KIND_VIEW, CONF_DEVICE_ID: device.id},
            options={CONF_MODE: MODE_ISOLATED, CONF_VERIFY_SSL: False, **options},
        ),
    )
    # As Home Assistant does when a flow creates an entry: its offer is done
    if (flow_id := offers(hass).get(device.id)) is not None:
        hass.config_entries.flow.async_abort(flow_id)
    return entry


async def add_manual_web_ui(hass: HomeAssistant, title: str, url: str) -> MockConfigEntry:
    return await _setup(
        hass,
        MockConfigEntry(
            domain=DOMAIN,
            version=2,
            title=title,
            data={CONF_KIND: KIND_VIEW},
            options={
                CONF_URL: url,
                CONF_MODE: MODE_ISOLATED,
                CONF_VERIFY_SSL: True,
                CONF_SHOW_IN_SIDEBAR: False,
            },
        ),
    )


def hub_of(hass: HomeAssistant) -> LocalWebUiHub:
    return hass.data[DOMAIN]


async def set_options(hass: HomeAssistant, entry: MockConfigEntry, **options: Any) -> None:
    """Change options the way the options flows do (applied in place, no reload)."""
    hub = hub_of(hass)
    hass.config_entries.async_update_entry(entry, options={**entry.options, **options})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is hub


async def set_visit_link(hass: HomeAssistant, entry: MockConfigEntry, choice: str) -> None:
    """Choose the device's "Visit" link in the web UI's options flow."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "web_ui"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MODE: MODE_ISOLATED,
            CONF_TRUSTED_ACK: False,
            CONF_VISIT_LINK: choice,
            CONF_VERIFY_SSL: False,
            CONF_SHOW_IN_SIDEBAR: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()


def offers(hass: HomeAssistant) -> dict[str, str]:
    """Pending discovery flows: device id -> flow id."""
    return {
        flow["context"]["unique_id"].removeprefix("device:"): flow["flow_id"]
        for flow in hass.config_entries.flow.async_progress_by_handler(
            DOMAIN, match_context={"source": SOURCE_INTEGRATION_DISCOVERY}
        )
    }


async def offer_details(hass: HomeAssistant, flow_id: str) -> dict[str, str]:
    """What the confirmation form of a discovered web UI shows."""
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "discovery_confirm"
    return dict(result["description_placeholders"])


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
# (1) discovery.parse_http_url / is_local_ui_url / is_lan_address / LanOnlyResolver
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
        ("homeassistant://local-web-ui/abc", None),
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
        pytest.param("http://app.localhost/", id="rfc6761-localhost-subdomain"),
        pytest.param("http://[fd20:ce::254]/", id="gcp-metadata-ipv6"),
        pytest.param("http://100.100.100.100/", id="tailscale-dns"),
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


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("192.168.1.50", True),
        ("10.1.2.3", True),
        ("172.16.0.1", True),
        ("100.64.0.1", True),  # CGNAT (Tailscale)
        ("fd12:3456:789a::1", True),
        ("::ffff:192.168.1.50", True),  # IPv4-mapped LAN address
        ("8.8.8.8", False),
        ("2001:4860:4860::8888", False),
        ("127.0.0.1", False),
        ("::1", False),
        ("::ffff:127.0.0.1", False),
        ("169.254.1.1", False),
        ("fe80::1", False),
        ("169.254.169.254", False),
        ("fd00:ec2::254", False),
        ("100.100.100.200", False),
        ("172.30.32.2", False),
        ("::ffff:172.30.32.2", False),
        ("224.0.0.251", False),
        ("0.0.0.0", False),
        ("::", False),
        # IPv6 transition prefixes carry traffic to other networks
        ("2002:c0a8:0132::1", False),  # 6to4
        ("2001:0:4136:e378::1", False),  # Teredo
        ("64:ff9b::c0a8:132", False),  # NAT64
        ("64:ff9b:1::1", False),
    ],
)
def test_is_lan_address(address: str, expected: bool) -> None:
    assert is_lan_address(ipaddress.ip_address(address)) is expected


def _resolved(*hosts: str) -> list[dict[str, Any]]:
    return [
        {
            "hostname": "device.local",
            "host": host,
            "port": 80,
            "family": socket.AF_INET6 if ":" in host else socket.AF_INET,
            "proto": 0,
            "flags": 0,
        }
        for host in hosts
    ]


async def test_lan_only_resolver_keeps_lan_addresses() -> None:
    inner = AsyncMock()
    inner.resolve.return_value = _resolved(
        "8.8.8.8", "192.168.1.50", "127.0.0.1", "fe80::1%eth0", "fd00::5", "not-an-ip"
    )
    result = await LanOnlyResolver(inner).resolve("device.local", 80, socket.AF_UNSPEC)
    assert [r["host"] for r in result] == ["192.168.1.50", "fd00::5"]
    inner.resolve.assert_awaited_once_with("device.local", 80, socket.AF_UNSPEC)


@pytest.mark.parametrize(
    "hosts",
    [
        pytest.param(("127.0.0.1",), id="loopback"),
        pytest.param(("172.30.32.2", "169.254.169.254"), id="supervisor-and-metadata"),
        pytest.param(("93.184.216.34",), id="public"),
        pytest.param((), id="nothing"),
    ],
)
async def test_lan_only_resolver_refuses_names_without_lan_address(hosts: tuple[str, ...]) -> None:
    """A name pointed at Home Assistant itself or the internet cannot be connected to."""
    inner = AsyncMock()
    inner.resolve.return_value = _resolved(*hosts)
    with pytest.raises(OSError, match="does not resolve to a local network address"):
        await LanOnlyResolver(inner).resolve("evil.local", 80)


async def test_lan_only_resolver_leaves_shared_resolver_open() -> None:
    inner = AsyncMock()
    await LanOnlyResolver(inner).close()
    inner.close.assert_not_called()


# ---------------------------------------------------------------------------
# (2) which devices are offered as discovered web UIs
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("http")
async def test_offers_devices_with_a_local_web_page(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL, name="Porch light")
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL, name="WLED")
    add_device(device_registry, owner, "cloud", "https://cloud.example.com/device/1")
    add_device(device_registry, owner, "no-url", None)
    add_device(device_registry, owner, "app", "homeassistant://hassio/addon/core_ssh")
    add_device(device_registry, owner, "loopback", "http://127.0.0.1:8080/")
    add_device(device_registry, owner, "supervisor", "http://172.30.32.2/")
    disabled = add_device(device_registry, owner, "disabled", "http://192.168.1.52/")
    device_registry.async_update_device(disabled.id, disabled_by=dr.DeviceEntryDisabler.USER)

    await setup_hub(hass)

    pending = offers(hass)
    assert set(pending) == {porch.id, kitchen.id}
    # The owning integration is not loaded, so only the host is shown
    assert await offer_details(hass, pending[porch.id]) == {
        "name": "Porch light",
        "url": PORCH_URL,
        "subtitle": "192.168.1.50",
    }
    assert await offer_details(hass, pending[kitchen.id]) == {
        "name": "WLED",
        "url": KITCHEN_URL,
        "subtitle": "wled-kitchen.local",
    }
    # Offers are not web UIs yet, and nothing is linked for them
    assert hub_of(hass).views == {}
    assert url_of(device_registry, porch.id) == PORCH_URL


@pytest.mark.usefixtures("http")
async def test_offer_name_and_subtitle(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    mock_integration(hass, MockModule(OWNER_DOMAIN, partial_manifest={"name": "Fake Devices"}))
    unnamed = add_device(device_registry, owner, "unnamed", "http://10.0.0.7:8080/")
    device_registry.async_update_device(unnamed.id, name=None)
    renamed = add_device(device_registry, owner, "renamed", "http://10.0.0.8/", name="WLED")
    device_registry.async_update_device(renamed.id, name_by_user="Kitchen strip")

    await setup_hub(hass)

    pending = offers(hass)
    assert await offer_details(hass, pending[unnamed.id]) == {
        "name": "Fake Devices · 10.0.0.7",
        "url": "http://10.0.0.7:8080/",
        "subtitle": "Fake Devices · 10.0.0.7",
    }
    assert (await offer_details(hass, pending[renamed.id]))["name"] == "Kitchen strip"


@pytest.mark.usefixtures("http")
async def test_accepting_an_offer_adds_the_web_ui(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL, name="Porch light")
    await setup_hub(hass)

    result = await hass.config_entries.flow.async_configure(offers(hass)[porch.id], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.title == "Porch light"
    assert entry.unique_id == f"device:{porch.id}"
    assert entry.data == {CONF_KIND: KIND_VIEW, CONF_DEVICE_ID: porch.id}
    assert entry.options == {CONF_MODE: MODE_ISOLATED, CONF_VERIFY_SSL: False}

    view = hub_of(hass).get_view(entry.entry_id)
    assert view is not None
    assert view.url == PORCH_URL
    assert view.device_id == porch.id
    assert view.source == "device"
    assert url_of(device_registry, porch.id) == link_for(entry.entry_id)
    assert offers(hass) == {}


@pytest.mark.usefixtures("http")
async def test_discovery_excludes_home_assistant_itself(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    await hass.config.async_update(
        internal_url="http://192.168.1.10:8123", external_url="https://ha.example.com"
    )
    add_device(device_registry, owner, "own", "http://192.168.1.10:8123/")
    other_port = add_device(device_registry, owner, "other-port", "http://192.168.1.10:8080/")

    await setup_hub(hass)
    assert set(offers(hass)) == {other_port.id}


@pytest.mark.usefixtures("http")
async def test_own_devices_are_not_offered(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    hub_entry = await setup_hub(hass)
    web_ui = await add_device_web_ui(hass, porch)
    # A device of ours with a local link of its own (none would normally have one)
    stray = device_registry.async_get_or_create(
        config_entry_id=web_ui.entry_id,
        identifiers={(DOMAIN, "stray")},
        configuration_url="http://192.168.1.77/",
    )
    await hass.async_block_till_done()
    assert offers(hass) == {}

    # Not on a full re-check either
    await set_options(hass, hub_entry, **{CONF_DISCOVERY: False})
    await set_options(hass, hub_entry, **{CONF_DISCOVERY: True})
    assert offers(hass) == {}
    assert url_of(device_registry, stray.id) == "http://192.168.1.77/"
    assert stray.id not in hub_of(hass).originals


@pytest.mark.usefixtures("http")
async def test_offer_looks_through_our_own_link(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """A device still linked to a web UI that is gone is offered with its own URL."""
    porch = add_device(device_registry, owner, "porch", link_for("gone"))
    unknown = add_device(device_registry, owner, "unknown", link_for("other"))
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "key": STORAGE_KEY,
        "data": {"originals": {porch.id: PORCH_URL}},
    }

    await setup_hub(hass)

    pending = offers(hass)
    assert set(pending) == {porch.id}
    assert (await offer_details(hass, pending[porch.id]))["url"] == PORCH_URL
    # Its page gets its own link back; nothing is known for the other one
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert url_of(device_registry, unknown.id) == link_for("other")
    assert hub_of(hass).originals == {}


@pytest.mark.usefixtures("http")
async def test_one_offer_per_url(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    """One physical device known to two integrations is offered once."""
    other = MockConfigEntry(domain="other_devices")
    other.add_to_hass(hass)
    first = add_device(device_registry, owner, "porch", PORCH_URL)
    second = device_registry.async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("other_devices", "porch")},
        name="Porch (other)",
        configuration_url=PORCH_URL,
    )
    admin = add_device(device_registry, owner, "porch-admin", "http://192.168.1.50/admin")

    await setup_hub(hass)
    pending = offers(hass)
    assert len(pending) == 2
    assert admin.id in pending
    assert len({first.id, second.id} & set(pending)) == 1


@pytest.mark.usefixtures("http")
async def test_url_used_by_a_web_ui_is_not_offered(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    other = MockConfigEntry(domain="other_devices")
    other.add_to_hass(hass)
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    twin = device_registry.async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("other_devices", "porch")},
        configuration_url=PORCH_URL,
    )
    router = add_device(device_registry, owner, "router", "http://192.168.1.1/")
    await add_device_web_ui(hass, porch)
    await add_manual_web_ui(hass, "Router", "http://192.168.1.1/")

    await setup_hub(hass)
    assert offers(hass) == {}

    # Not later either, when the devices change
    device_registry.async_update_device(router.id, name_by_user="Router")
    device_registry.async_update_device(twin.id, name_by_user="Porch twin")
    await hass.async_block_till_done()
    assert offers(hass) == {}


@pytest.mark.usefixtures("http")
async def test_configured_or_ignored_devices_are_not_offered(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    ignored = add_device(device_registry, owner, "ignored", "http://192.168.1.60/")
    fresh = add_device(device_registry, owner, "fresh", "http://192.168.1.61/")
    MockConfigEntry(
        domain=DOMAIN,
        version=2,
        source=SOURCE_IGNORE,
        unique_id=device_unique_id(ignored.id),
        title="Ignored",
    ).add_to_hass(hass)
    await add_device_web_ui(hass, porch)

    hub_entry = await setup_hub(hass)
    assert set(offers(hass)) == {fresh.id}

    # Ignoring an offer keeps it away too, also on a full re-check
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IGNORE},
        data={"unique_id": device_unique_id(fresh.id), "title": "Fresh"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert offers(hass) == {}
    await set_options(hass, hub_entry, **{CONF_DISCOVERY: False})
    await set_options(hass, hub_entry, **{CONF_DISCOVERY: True})
    device_registry.async_update_device(ignored.id, name_by_user="Still ignored")
    await hass.async_block_till_done()
    assert offers(hass) == {}


@pytest.mark.usefixtures("http")
async def test_each_device_is_offered_once_per_run(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    hub_entry = await setup_hub(hass)
    [flow_id] = offers(hass).values()

    # Dismissed: not offered again for changes or refreshes
    hass.config_entries.flow.async_abort(flow_id)
    device_registry.async_update_device(porch.id, name_by_user="Porch")
    device_registry.async_update_device(porch.id, configuration_url="http://192.168.1.51/")
    await set_options(hass, hub_entry, **{CONF_LINK_DEVICE_PAGES: False})
    await add_manual_web_ui(hass, "NAS", "http://nas.lan/")
    await hass.async_block_till_done()
    assert offers(hass) == {}


@pytest.mark.usefixtures("http")
async def test_device_offered_when_it_gains_a_local_url(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", None)
    cloud = add_device(device_registry, owner, "cloud", "https://cloud.example.com/1")
    await setup_hub(hass)
    assert offers(hass) == {}

    device_registry.async_update_device(porch.id, configuration_url=PORCH_URL)
    device_registry.async_update_device(cloud.id, configuration_url="http://192.168.1.70/")
    late = add_device(device_registry, owner, "late", "http://192.168.1.71/")
    await hass.async_block_till_done()
    assert set(offers(hass)) == {porch.id, cloud.id, late.id}

    # Enabled later
    disabled = add_device(
        device_registry,
        owner,
        "disabled",
        "http://192.168.1.72/",
        disabled_by=dr.DeviceEntryDisabler.USER,
    )
    await hass.async_block_till_done()
    assert disabled.id not in offers(hass)
    device_registry.async_update_device(disabled.id, disabled_by=None)
    await hass.async_block_till_done()
    assert disabled.id in offers(hass)


@pytest.mark.usefixtures("http")
async def test_no_offers_without_hub(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    await add_manual_web_ui(hass, "NAS", "http://nas.lan/")
    add_device(device_registry, owner, "late", "http://192.168.1.71/")
    device_registry.async_update_device(porch.id, name_by_user="Porch")
    await hass.async_block_till_done()
    assert hub_of(hass).hub_entry is None
    assert offers(hass) == {}


@pytest.mark.usefixtures("http")
async def test_no_offers_with_discovery_off(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    hub_entry = await setup_hub(hass, **{CONF_DISCOVERY: False})
    add_device(device_registry, owner, "late", "http://192.168.1.71/")
    device_registry.async_update_device(porch.id, name_by_user="Porch")
    await hass.async_block_till_done()
    assert offers(hass) == {}

    await set_options(hass, hub_entry, **{CONF_DISCOVERY: True})
    assert len(offers(hass)) == 2


@pytest.mark.usefixtures("http")
async def test_turning_discovery_off_aborts_pending_offers(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    hub_entry = await setup_hub(hass)
    assert set(offers(hass)) == {porch.id, kitchen.id}

    await set_options(hass, hub_entry, **{CONF_DISCOVERY: False})
    assert offers(hass) == {}

    # Turned back on: offered again
    await set_options(hass, hub_entry, **{CONF_DISCOVERY: True})
    assert set(offers(hass)) == {porch.id, kitchen.id}

    # Unloading the hub ends them too, even while a web UI stays loaded
    await add_manual_web_ui(hass, "NAS", "http://nas.lan/")
    assert await hass.config_entries.async_unload(hub_entry.entry_id)
    await hass.async_block_till_done()
    assert offers(hass) == {}
    assert hub_of(hass).active


@pytest.mark.usefixtures("http")
async def test_deleted_web_ui_is_offered_again(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    await setup_hub(hass)
    result = await hass.config_entries.flow.async_configure(offers(hass)[porch.id], {})
    entry = result["result"]
    await hass.async_block_till_done()
    assert offers(hass) == {}

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert set(offers(hass)) == {porch.id}


@pytest.mark.usefixtures("http")
async def test_unrelated_registry_changes_only_recheck_that_device(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    other = add_device(device_registry, owner, "other", None)
    await setup_hub(hass)
    await add_device_web_ui(hass, porch)
    hub = hub_of(hass)

    with (
        patch.object(hub, "async_refresh", wraps=hub.async_refresh) as refresh,
        patch.object(hub, "async_discover", wraps=hub.async_discover) as discover,
    ):
        device_registry.async_update_device(other.id, name_by_user="Other")
        # Changes that cannot matter are not looked at
        device_registry.async_update_device(other.id, sw_version="2.0")
        await hass.async_block_till_done()
        refresh.assert_not_called()
        assert discover.call_count == 1
        assert discover.call_args.kwargs["only"].id == other.id

        # The device of a web UI refreshes it
        device_registry.async_update_device(porch.id, name_by_user="Porch")
        await hass.async_block_till_done()
        assert refresh.call_count == 1


# ---------------------------------------------------------------------------
# (3) device page ("Visit") links
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("http")
async def test_visit_link_follows_hub_option(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    cloud = add_device(device_registry, owner, "cloud", "https://cloud.example.com/1")
    hub_entry = await setup_hub(hass)
    entry = await add_device_web_ui(hass, porch)
    hub = hub_of(hass)

    assert url_of(device_registry, porch.id) == link_for(entry.entry_id)
    assert url_of(device_registry, cloud.id) == "https://cloud.example.com/1"
    assert hub.originals == {porch.id: PORCH_URL}
    await flush_storage(hass, freezer)
    assert hass_storage[STORAGE_KEY]["data"]["originals"] == {porch.id: PORCH_URL}

    registry_events.clear()
    await set_options(hass, hub_entry, **{CONF_LINK_DEVICE_PAGES: False})
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert hub.originals == {}
    assert len(updates_for(registry_events, porch.id)) == 1
    await flush_storage(hass, freezer)
    assert hass_storage[STORAGE_KEY]["data"]["originals"] == {}

    await set_options(hass, hub_entry, **{CONF_LINK_DEVICE_PAGES: True})
    assert url_of(device_registry, porch.id) == link_for(entry.entry_id)
    assert hub.originals == {porch.id: PORCH_URL}


@pytest.mark.usefixtures("http")
async def test_visit_link_without_hub_opens_here(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    entry = await add_device_web_ui(hass, porch)
    assert hub_of(hass).hub_entry is None
    assert url_of(device_registry, porch.id) == link_for(entry.entry_id)


@pytest.mark.usefixtures("http")
@pytest.mark.parametrize("hub_default", [True, False])
async def test_visit_link_choice_of_the_web_ui(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    hub_default: bool,
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    hub_entry = await setup_hub(hass, **{CONF_LINK_DEVICE_PAGES: hub_default})
    entry = await add_device_web_ui(hass, porch)
    hub = hub_of(hass)
    linked = link_for(entry.entry_id)

    await set_visit_link(hass, entry, VISIT_HERE)
    assert entry.options[CONF_VISIT_LINK] == VISIT_HERE
    assert url_of(device_registry, porch.id) == linked
    assert hub.originals == {porch.id: PORCH_URL}

    await set_visit_link(hass, entry, VISIT_DEVICE)
    assert entry.options[CONF_VISIT_LINK] == VISIT_DEVICE
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert hub.originals == {}

    # The hub option does not override an explicit choice
    await set_options(hass, hub_entry, **{CONF_LINK_DEVICE_PAGES: not hub_default})
    assert url_of(device_registry, porch.id) == PORCH_URL

    # Back to the default: not stored, and the hub option applies again
    await set_options(hass, hub_entry, **{CONF_LINK_DEVICE_PAGES: hub_default})
    await set_visit_link(hass, entry, "default")
    assert CONF_VISIT_LINK not in entry.options
    assert url_of(device_registry, porch.id) == (linked if hub_default else PORCH_URL)
    assert set(hub.originals) == ({porch.id} if hub_default else set())


@pytest.mark.usefixtures("http")
async def test_disabling_web_ui_restores_link_and_ends_sessions(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    hass_admin_user: Any,
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    await setup_hub(hass)
    entry = await add_device_web_ui(hass, porch)
    other = await add_device_web_ui(hass, kitchen)
    hub = hub_of(hass)
    session = hub.sessions.create(entry.entry_id, hass_admin_user.id, None)
    other_session = hub.sessions.create(other.entry_id, hass_admin_user.id, None)
    seed_site_data(hub, hass_admin_user.id, hub.get_view(entry.entry_id))

    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()

    assert url_of(device_registry, porch.id) == PORCH_URL
    assert hub.originals == {kitchen.id: KITCHEN_URL}
    assert hub.get_view(entry.entry_id) is None
    assert hub.sessions.get(session.token) is None
    assert hub.sessions.touch(other_session.token, other.entry_id) is not None
    # Its device is not offered while its (disabled) entry exists
    assert offers(hass) == {}

    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done()
    assert url_of(device_registry, porch.id) == link_for(entry.entry_id)
    # Disabling is not deleting: site data is kept
    assert has_site_data(hub, hass_admin_user.id, hub.get_view(entry.entry_id))


@pytest.mark.usefixtures("http")
async def test_disabling_web_ui_restores_unsaved_original(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Links are restored from the originals in memory, not from what was saved."""
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    await setup_hub(hass)
    entry = await add_device_web_ui(hass, porch)
    await flush_storage(hass, freezer)
    assert hass_storage[STORAGE_KEY]["data"]["originals"] == {porch.id: PORCH_URL}

    # Changed right before disabling: the delayed save has not run yet
    device_registry.async_update_device(porch.id, configuration_url="http://192.168.1.84/")
    await hass.async_block_till_done()
    assert url_of(device_registry, porch.id) == link_for(entry.entry_id)
    assert hass_storage[STORAGE_KEY]["data"]["originals"] == {porch.id: PORCH_URL}

    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert url_of(device_registry, porch.id) == "http://192.168.1.84/"


@pytest.mark.usefixtures("http")
async def test_deleting_web_ui_restores_link_and_forgets_site_data(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    await setup_hub(hass)
    entry = await add_device_web_ui(hass, porch)
    nas = await add_manual_web_ui(hass, "NAS", "http://nas.lan:5000/")
    hub = hub_of(hass)
    views = {"porch": hub.get_view(entry.entry_id), "nas": hub.get_view(nas.entry_id)}
    sessions = {}
    for user_id in ("alice", "bob"):
        for name, view in views.items():
            seed_site_data(hub, user_id, view)
            sessions[user_id, name] = hub.sessions.create(view.view_id, user_id, None)
    await flush_storage(hass, freezer)
    assert len(stored_site_keys(hass_storage)) == 4

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert url_of(device_registry, porch.id) == PORCH_URL
    assert hub.originals == {}
    assert hub.get_view(entry.entry_id) is None
    for user_id in ("alice", "bob"):
        assert not has_site_data(hub, user_id, views["porch"])
        assert has_site_data(hub, user_id, views["nas"])
        assert hub.sessions.get(sessions[user_id, "porch"].token) is None
        assert hub.sessions.get(sessions[user_id, "nas"].token) is not None
    await flush_storage(hass, freezer)
    assert stored_site_keys(hass_storage) == {f"{user}|{nas.entry_id}" for user in ("alice", "bob")}
    assert hass_storage[STORAGE_KEY]["data"]["originals"] == {}


@pytest.mark.usefixtures("http")
@pytest.mark.parametrize(
    "hub_last",
    [
        True,
        False,
    ],
)
async def test_deleting_last_entry_restores_links_and_removes_storage(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
    hub_last: bool,
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    cloud = add_device(device_registry, owner, "cloud", "https://cloud.example.com/d/1")
    hub_entry = await setup_hub(hass)
    porch_ui = await add_device_web_ui(hass, porch)
    kitchen_ui = await add_device_web_ui(hass, kitchen)
    seed_site_data(hub_of(hass), "alice", hub_of(hass).get_view(porch_ui.entry_id))
    await flush_storage(hass, freezer)
    assert hass_storage[STORAGE_KEY]["data"]["originals"] == {
        porch.id: PORCH_URL,
        kitchen.id: KITCHEN_URL,
    }
    assert STORAGE_KEY_JAR in hass_storage

    order = [porch_ui, kitchen_ui, hub_entry] if hub_last else [hub_entry, porch_ui, kitchen_ui]
    for entry in order:
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert url_of(device_registry, porch.id) == PORCH_URL
    assert url_of(device_registry, kitchen.id) == KITCHEN_URL
    assert url_of(device_registry, cloud.id) == "https://cloud.example.com/d/1"
    assert STORAGE_KEY not in hass_storage
    assert STORAGE_KEY_JAR not in hass_storage
    assert not hub_of(hass).active

    # Nothing listens any more, and no delayed save brings the storage back
    device_registry.async_update_device(porch.id, configuration_url="http://192.168.1.81/")
    await flush_storage(hass, freezer)
    assert url_of(device_registry, porch.id) == "http://192.168.1.81/"
    assert STORAGE_KEY not in hass_storage
    assert STORAGE_KEY_JAR not in hass_storage


@pytest.mark.usefixtures("http")
async def test_integration_resets_its_url(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    registry_events: list[Event[dict[str, Any]]],
) -> None:
    """The owning integration writes its own URL again (say, on reload): relinked."""
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    await setup_hub(hass)
    entry = await add_device_web_ui(hass, porch)
    hub = hub_of(hass)

    registry_events.clear()
    device_registry.async_update_device(porch.id, configuration_url=PORCH_URL)
    await hass.async_block_till_done()
    assert url_of(device_registry, porch.id) == link_for(entry.entry_id)
    assert hub.originals == {porch.id: PORCH_URL}
    assert len(updates_for(registry_events, porch.id)) == 2  # theirs, then ours

    # With the link off, their URL just stays
    await set_visit_link(hass, entry, VISIT_DEVICE)
    device_registry.async_update_device(porch.id, configuration_url="http://192.168.1.51/")
    await hass.async_block_till_done()
    assert url_of(device_registry, porch.id) == "http://192.168.1.51/"
    assert hub.originals == {}
    assert hub.get_view(entry.entry_id).url == "http://192.168.1.51/"


@pytest.mark.usefixtures("http")
async def test_device_url_change_moves_view_and_forgets_old_site_data(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    await setup_hub(hass)
    entry = await add_device_web_ui(hass, porch)
    hub = hub_of(hass)
    view = hub.get_view(entry.entry_id)
    seed_site_data(hub, "alice", view)

    # Same site, other page: the data stays
    device_registry.async_update_device(porch.id, configuration_url="http://192.168.1.50/app/")
    await hass.async_block_till_done()
    moved = hub.get_view(entry.entry_id)
    assert moved.url == "http://192.168.1.50/app/"
    assert moved.entry == "/app/"
    assert url_of(device_registry, porch.id) == link_for(entry.entry_id)
    assert hub.originals == {porch.id: "http://192.168.1.50/app/"}
    assert has_site_data(hub, "alice", moved)

    # Another site: the old one's cookies and storage must not reach it
    device_registry.async_update_device(porch.id, configuration_url="http://192.168.1.99:8080/")
    await hass.async_block_till_done()
    moved = hub.get_view(entry.entry_id)
    assert str(moved.origin) == "http://192.168.1.99:8080"
    assert url_of(device_registry, porch.id) == link_for(entry.entry_id)
    assert hub.originals == {porch.id: "http://192.168.1.99:8080/"}
    assert not has_site_data(hub, "alice", moved)
    await flush_storage(hass, freezer)
    assert stored_site_keys(hass_storage) == set()


@pytest.mark.usefixtures("http")
@pytest.mark.parametrize("url", [None, "https://cloud.example.com/1", "http://127.0.0.1/"])
async def test_device_url_no_longer_local(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    url: str | None,
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    await setup_hub(hass)
    entry = await add_device_web_ui(hass, porch)
    hub = hub_of(hass)

    device_registry.async_update_device(porch.id, configuration_url=url)
    await hass.async_block_till_done()
    assert hub.get_view(entry.entry_id) is None
    assert entry.state is ConfigEntryState.LOADED
    assert url_of(device_registry, porch.id) == url
    assert hub.originals == {}

    device_registry.async_update_device(porch.id, configuration_url=PORCH_URL)
    await hass.async_block_till_done()
    assert hub.get_view(entry.entry_id) is not None
    assert url_of(device_registry, porch.id) == link_for(entry.entry_id)


@pytest.mark.usefixtures("http")
async def test_device_removed(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    kitchen = add_device(device_registry, owner, "kitchen", KITCHEN_URL)
    await setup_hub(hass)
    entry = await add_device_web_ui(hass, porch)
    await add_device_web_ui(hass, kitchen)
    hub = hub_of(hass)

    device_registry.async_remove_device(porch.id)
    await hass.async_block_till_done()
    assert hub.get_view(entry.entry_id) is None
    assert hub.view_for_device(porch.id) is None
    assert hub.originals == {kitchen.id: KITCHEN_URL}
    assert entry.state is ConfigEntryState.LOADED
    await flush_storage(hass, freezer)
    assert hass_storage[STORAGE_KEY]["data"]["originals"] == {kitchen.id: KITCHEN_URL}


@pytest.mark.usefixtures("http")
async def test_device_disabled(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    await setup_hub(hass)
    entry = await add_device_web_ui(hass, porch)
    hub = hub_of(hass)

    device_registry.async_update_device(porch.id, disabled_by=dr.DeviceEntryDisabler.USER)
    await hass.async_block_till_done()
    assert hub.get_view(entry.entry_id) is None
    assert url_of(device_registry, porch.id) == PORCH_URL
    assert hub.originals == {}

    device_registry.async_update_device(porch.id, disabled_by=None)
    await hass.async_block_till_done()
    assert hub.get_view(entry.entry_id) is not None
    assert url_of(device_registry, porch.id) == link_for(entry.entry_id)


@pytest.mark.usefixtures("http")
async def test_device_turned_into_child_device(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, owner: MockConfigEntry
) -> None:
    """A device converted to a child device has no web page of its own any more."""
    parent = add_device(device_registry, owner, "parent", "http://192.168.1.40/")
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    await setup_hub(hass, **{CONF_DISCOVERY: False})
    entry = await add_device_web_ui(hass, porch)
    hub = hub_of(hass)
    assert hub.originals == {porch.id: PORCH_URL}

    # Only devices not registered again in this run can become child devices
    device_registry.async_config_entry_unloaded(owner.entry_id)
    child = device_registry.async_get_or_create_child(
        config_entry_id=owner.entry_id,
        identifiers={(OWNER_DOMAIN, "porch")},
        parent_device_id=parent.id,
    )
    assert child.id == porch.id
    await hass.async_block_till_done()
    assert hub.get_view(entry.entry_id) is None
    assert hub.originals == {}


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
    hub_entry = await setup_hub(hass)
    entry = await add_device_web_ui(hass, porch)
    hub = hub_of(hass)

    registry_events.clear()
    child = device_registry.async_get_or_create_child(
        config_entry_id=owner.entry_id,
        identifiers={(OWNER_DOMAIN, "porch-relay")},
        name="Relay",
        parent_device_id=porch.id,
    )
    device_registry.async_update_child_device(child.id, name_by_user="Porch relay")
    device_registry.async_update_child_device(child.id, disabled_by=dr.DeviceEntryDisabler.USER)
    await hass.async_block_till_done()
    assert hub.view_for_device(child.id) is None
    assert hub.area_name(child.id) is None
    assert child.id not in hub.originals
    assert offers(hass) == {}
    assert updates_for(registry_events, porch.id) == []

    # A web UI entry pointing at a child device has no view
    child_ui = await add_device_web_ui(hass, child, title="Relay")
    assert hub.get_view(child_ui.entry_id) is None

    # Full re-syncs walk the registry without touching child devices
    await set_options(hass, hub_entry, **{CONF_LINK_DEVICE_PAGES: False})
    await set_options(hass, hub_entry, **{CONF_LINK_DEVICE_PAGES: True})
    await set_options(hass, hub_entry, **{CONF_DISCOVERY: False})
    await set_options(hass, hub_entry, **{CONF_DISCOVERY: True})
    device_registry.async_remove_device(child.id)
    await hass.async_block_till_done()

    assert url_of(device_registry, porch.id) == link_for(entry.entry_id)
    assert set(hub.views) == {entry.entry_id}
    assert offers(hass) == {}
    assert "ChildDeviceEntry" not in caplog.text
    assert "Detected that custom integration" not in caplog.text


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
    others = [
        add_device(device_registry, owner, f"other{i}", f"http://192.168.3.{i + 10}/")
        for i in range(5)
    ]
    await setup_hub(hass)
    entries = [await add_device_web_ui(hass, device) for device in devices]
    registry_events.clear()
    for entry in entries:
        await set_visit_link(hass, entry, VISIT_DEVICE)
        await set_visit_link(hass, entry, VISIT_HERE)
    # per web UI: unlink, link
    assert len(registry_events) == 2 * len(devices)

    registry_events.clear()
    for i, (device, entry) in enumerate(zip(devices, entries, strict=True)):
        # Each its own URL: devices sharing one would share a single offer
        device_registry.async_update_device(device.id, configuration_url=f"http://10.1.1.{i}/")
        device_registry.async_update_device(device.id, name_by_user="renamed")
        hass.config_entries.async_update_entry(entry, title=f"Web UI {i}")
    for device in others:
        device_registry.async_update_device(device.id, name_by_user="renamed")
    await hass.async_block_till_done()
    # per web UI: theirs + relink, rename, rename of its own device; per other: rename
    assert len(registry_events) == 4 * len(devices) + len(others)
    for device, entry in zip(devices, entries, strict=True):
        assert url_of(device_registry, device.id) == link_for(entry.entry_id)
    assert len(offers(hass)) == len(others)


@pytest.mark.usefixtures("http")
async def test_user_removal_forgets_sessions_and_site_data(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    owner: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    porch = add_device(device_registry, owner, "porch", PORCH_URL)
    await setup_hub(hass)
    entry = await add_device_web_ui(hass, porch)
    hub = hub_of(hass)
    view = hub.get_view(entry.entry_id)
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
    assert hub.sessions.touch(sessions[staying.id].token, view.view_id) is not None
    assert not has_site_data(hub, leaving.id, view)
    assert has_site_data(hub, staying.id, view)
    await flush_storage(hass, freezer)
    assert stored_site_keys(hass_storage) == {f"{staying.id}|{view.view_id}"}


@pytest.mark.usefixtures("http")
async def test_users_removed_while_not_running_are_forgotten(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
    hass_admin_user: Any,
) -> None:
    hass_storage[STORAGE_KEY_JAR] = {
        "version": 1,
        "key": STORAGE_KEY_JAR,
        "data": {
            "cookies": {"gone-user|abc": [], f"{hass_admin_user.id}|abc": []},
            "storage": {"gone-user|abc": {"a": "1"}, f"{hass_admin_user.id}|abc": {"b": "2"}},
        },
    }
    await setup_hub(hass)
    hub = hub_of(hass)
    assert hub.shim_storage("gone-user", "abc") == {}
    assert hub.shim_storage(hass_admin_user.id, "abc") == {"b": "2"}
    await flush_storage(hass, freezer)
    assert stored_site_keys(hass_storage) == {f"{hass_admin_user.id}|abc"}


@pytest.mark.usefixtures("http")
async def test_stores_are_private(hass: HomeAssistant) -> None:
    """Stored cookies and site storage hold device credentials: owner-only files."""
    with patch("custom_components.local_web_ui.hub.Store", wraps=Store) as store_cls:
        await setup_hub(hass)
    keys = set()
    for call in store_cls.call_args_list:
        keys.add(call.args[2])
        assert call.kwargs.get("private") is True, call
        assert call.kwargs.get("atomic_writes") is True, call
    assert keys == {STORAGE_KEY, STORAGE_KEY_JAR}
