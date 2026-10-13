# ruff: noqa: F811
"""Native device-page timestamp shares the canonical passive activity tracker."""

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from tests.test_feed import capable, received
from tests.test_integration import DEVICE, custom, feeder  # noqa: F401


async def test_last_feed_is_timestamp_on_same_physical_device(hass, feeder):
    sensor, entry, transport, device = feeder
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", "tuya_feeder", f"{DEVICE}_last_feed"
    )
    assert entity_id is not None
    row = registry.async_get(entity_id)
    meal = registry.async_get(sensor.entity_id)
    assert row.config_entry_id == entry.entry_id
    assert row.device_id == meal.device_id and row.device_id is not None
    physical = dr.async_get(hass).async_get(row.device_id)
    assert ("localtuya", f"local_{DEVICE}") in physical.identifiers
    state = hass.states.get(entity_id)
    assert state.state == "unknown"
    assert state.attributes["device_class"] == "timestamp"
    capable(hass, sensor, device)
    received(hass, 4, "standby")
    received(hass, 4, "feeding")
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert dt_util.parse_datetime(state.state) == dt_util.parse_datetime(
        sensor.manual.last_feed_at
    ).replace(microsecond=0)
    assert dt_util.parse_datetime(state.state).tzinfo is not None
    first = state.state
    received(hass, 14, 8)
    received(hass, 4, "feeding")
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == first
    transport.assert_not_called()


async def test_last_feed_entity_restores_same_time_without_feeding(hass, feeder):
    sensor, entry, transport, device = feeder
    capable(hass, sensor, device)
    received(hass, 4, "standby")
    received(hass, 4, "feeding")
    await hass.async_block_till_done()
    first = sensor.manual.last_feed_at
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", "tuya_feeder", f"{DEVICE}_last_feed"
    )
    assert entity_id is not None
    assert dt_util.parse_datetime(
        hass.states.get(entity_id).state
    ) == dt_util.parse_datetime(first).replace(microsecond=0)
    transport.assert_not_called()
