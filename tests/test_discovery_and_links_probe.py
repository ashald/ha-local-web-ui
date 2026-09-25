import logging
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events
from custom_components.local_web_ui.const import DOMAIN


async def test_probe(hass, device_registry, caplog):
    await async_setup_component(hass, "http", {})
    other = MockConfigEntry(domain="fake_dev", title="Fake")
    other.add_to_hass(hass)
    d = device_registry.async_get_or_create(config_entry_id=other.entry_id, identifiers={("fake_dev", "1")}, name="Porch", configuration_url="http://192.168.1.50/")
    entry = MockConfigEntry(domain=DOMAIN, title="Local Web UIs", options={})
    entry.add_to_hass(hass)
    events = async_capture_events(hass, dr.EVENT_DEVICE_REGISTRY_UPDATED)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hub = entry.runtime_data
    print("own", hub._own_hosts)
    print("url", device_registry.async_get(d.id).configuration_url, [e.data for e in events])
    print(hub.discovered_views())
    events.clear()
    child = device_registry.async_get_or_create_child(config_entry_id=other.entry_id, identifiers={("fake_dev", "1c")}, name="Child", parent_device_id=d.id)
    await hass.async_block_till_done()
    print("child events", [e.data for e in events])
    print("LOG", [r.getMessage() for r in caplog.records if "Child" in r.getMessage()])
    assert 0
