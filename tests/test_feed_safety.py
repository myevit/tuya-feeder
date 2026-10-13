# ruff: noqa: F811
"""Failure and provenance gates for one-shot feeding."""

import asyncio

import pytest
from homeassistant.exceptions import ServiceValidationError

from tests.test_feed import capable, received
from tests.test_integration import BASE, DEVICE, custom, feeder, report  # noqa: F401


async def test_idle_native_report_updates_manual_availability(hass, feeder):
    sensor, _, _, device = feeder
    wire = capable(hass, sensor, device)
    device.connected = False
    sensor._publish()
    assert not hass.states.get(sensor.entity_id).attributes["supports_manual_feed"]
    device.connected = True
    received(hass, 4, "standby")
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).attributes["supports_manual_feed"]
    wire.exchange_quick.assert_not_called()


async def test_feed_no_cached_or_out_of_order_confirmation(hass, feeder):
    sensor, _, _, device = feeder
    wire = capable(hass, sensor, device)
    device._status["14"] = 3

    async def send(*args, **kwargs):
        received(hass, 14, 3)
        received(hass, 4, "feeding", "other_synthetic_feeder")
        received(hass, 14, 3)

    wire.exchange_quick.side_effect = send
    result = await sensor.feed(3)
    assert result["state"] == "unknown"
    assert sensor.manual.observed_at is None


async def test_feed_exception_and_restart_preserve_unknown(hass, feeder):
    from custom_components.tuya_feeder.feed import ManualFeed

    sensor, _, _, device = feeder
    wire = capable(hass, sensor, device)
    wire.exchange_quick.side_effect = OSError("synthetic connection lost")
    result = await sensor.feed(3)
    assert result["state"] == "unknown"
    restored = ManualFeed(sensor)
    await restored.start()
    assert restored.uncertain and restored.state == "unknown"
    assert not restored._sent
    await restored.stop()
    wire.exchange_quick.assert_awaited_once()


async def test_feed_cancellation_never_retries(hass, feeder):
    sensor, _, _, device = feeder
    wire = capable(hass, sensor, device)
    entered = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    wire.exchange_quick.side_effect = send
    task = asyncio.create_task(sensor.feed(3))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sensor.manual.uncertain and not sensor.manual.pending
    with pytest.raises(ServiceValidationError):
        await sensor.feed(3)
    wire.exchange_quick.assert_awaited_once()


async def test_feed_and_schedule_share_guard(hass, feeder):
    sensor, _, transport, device = feeder
    wire = capable(hass, sensor, device)
    await report(hass, BASE)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()

    wire.exchange_quick.side_effect = send
    task = asyncio.create_task(sensor.feed(3))
    await entered.wait()
    with pytest.raises(ServiceValidationError):
        await sensor.set_schedule(1, BASE)
    with pytest.raises(ServiceValidationError):
        await sensor.meal_action("delete_meal", 1, 0)
    transport.assert_not_called()
    release.set()
    await task
    with pytest.raises(ServiceValidationError):
        await sensor.set_schedule(1, BASE)


@pytest.mark.parametrize(
    "gate",
    [
        "model",
        "offline",
        "optimistic",
        "session",
        "local_pending",
        "unknown_fault",
        "unaudited",
    ],
)
async def test_feed_unsupported_gate_no_write(hass, feeder, gate):
    sensor, _, _, device = feeder
    wire = capable(hass, sensor, device)
    if gate == "model":
        local = hass.config_entries.async_get_entry(
            sensor.entry.data["localtuya_entry_id"]
        )
        data = {
            "devices": {
                DEVICE: {**local.data["devices"][DEVICE], "model": "Unknown model"}
            }
        }
        hass.config_entries.async_update_entry(local, data=data)
    elif gate == "offline":
        device.connected = False
    elif gate == "optimistic":
        device.is_write_only = True
    elif gate == "session":
        wire.local_key = wire.real_local_key
    elif gate == "local_pending":
        device._pending_status = {"2": True}
    elif gate == "unknown_fault":
        device._status.pop("13")
    elif gate == "unaudited":
        sensor.audited = False
    with pytest.raises(ServiceValidationError):
        await sensor.feed(3)
    wire.exchange_quick.assert_not_called()
