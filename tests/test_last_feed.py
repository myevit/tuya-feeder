# ruff: noqa: F811
"""Passive feeding activity across command origins, never cache timestamps."""

from tests.test_feed import capable, received
from tests.test_integration import custom, feeder  # noqa: F401


async def test_spontaneous_feeding_edge_updates_global_time(hass, feeder):
    sensor, _, transport, device = feeder
    wire = capable(hass, sensor, device)
    received(hass, 4, "standby")
    await hass.async_block_till_done()
    received(hass, 4, "feeding")
    await hass.async_block_till_done()
    attrs = hass.states.get(sensor.entity_id).attributes
    assert attrs.get("last_feed_at")
    assert attrs["last_feed_source"] == "local_dp4_feeding_edge"
    assert attrs["feeding_requested_at"] is None
    first = attrs["last_feed_at"]
    received(hass, 14, 8)
    received(hass, 4, "feeding")
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).attributes["last_feed_at"] == first
    received(hass, 4, "done")
    received(hass, 4, "standby")
    received(hass, 4, "feeding")
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).attributes["last_feed_at"] > first
    transport.assert_not_called()
    wire.exchange_quick.assert_not_called()


async def test_feed_timestamp_persists_without_restoring_active_cycle(hass, feeder):
    from custom_components.tuya_feeder.feed import ManualFeed

    sensor, _, _, device = feeder
    capable(hass, sensor, device)
    received(hass, 4, "standby")
    received(hass, 4, "feeding")
    await hass.async_block_till_done()
    first = sensor.manual.last_feed_at
    restored = ManualFeed(sensor)
    await restored.start()
    assert restored.last_feed_at == first
    assert restored._activity_state is None
    assert restored.state == "idle"
    await restored.stop()


async def test_cached_full_status_dp14_and_unrelated_reports_never_date_feed(
    hass, feeder
):
    from tests.test_integration import DEVICE

    sensor, _, transport, device = feeder
    wire = capable(hass, sensor, device)
    for state in ("standby", "feeding", "standby"):
        wire.dispatched_dps = {"4": state, "14": 8, "13": 0}
        hass.bus.async_fire(
            "localtuya_status_update",
            {
                "device_id": DEVICE,
                "old_status": {"4": "standby"},
                "new_status": wire.dispatched_dps,
            },
        )
        received(hass, 14, 8)
        received(hass, 13, 0)
        received(hass, 1, "synthetic schedule update")
        await hass.async_block_till_done()
        assert sensor.manual.last_feed_at is None
    transport.assert_not_called()
    wire.exchange_quick.assert_not_called()


async def test_first_single_dp_edge_has_audited_old_status_after_initial_get(
    hass, feeder
):
    from tests.test_integration import DEVICE

    sensor, _, _, device = feeder
    wire = capable(hass, sensor, device)
    received(hass, 4, "feeding")  # No fabricated time from an unknown baseline.
    await hass.async_block_till_done()
    assert sensor.manual.last_feed_at is None
    wire.dispatched_dps = {"4": "feeding"}
    hass.bus.async_fire(
        "localtuya_status_update",
        {
            "device_id": DEVICE,
            "old_status": {"4": "standby"},
            "new_status": {"4": "feeding"},
        },
    )
    await hass.async_block_till_done()
    first = sensor.manual.last_feed_at
    assert first
    received(hass, 4, "feeding")
    received(hass, 14, 8)
    received(hass, 14, 8)
    await hass.async_block_till_done()
    assert sensor.manual.last_feed_at == first


async def test_reconnect_feeding_baseline_and_foreign_device_do_not_date_feed(
    hass, feeder
):
    sensor, _, _, device = feeder
    capable(hass, sensor, device)
    received(hass, 4, "standby")
    await hass.async_block_till_done()
    from types import SimpleNamespace

    device._interface = SimpleNamespace(dispatched_dps={"4": "feeding", "14": 8})
    received(hass, 4, "feeding")
    received(hass, 4, "standby", "foreign_synthetic_device")
    received(hass, 4, "feeding", "foreign_synthetic_device")
    await hass.async_block_till_done()
    assert sensor.manual.last_feed_at is None


async def test_manual_service_ack_does_not_date_but_actual_edge_does(hass, feeder):
    sensor, _, _, device = feeder
    wire = capable(hass, sensor, device)
    received(hass, 4, "standby")
    await hass.async_block_till_done()

    async def command(*args, **kwargs):
        assert sensor.manual.last_feed_at is None
        received(hass, 4, "feeding")
        received(hass, 14, 3)

    wire.exchange_quick.side_effect = command
    result = await sensor.feed(3)
    assert result["reported"]
    assert sensor.manual.last_feed_at == sensor.manual.observed_at
