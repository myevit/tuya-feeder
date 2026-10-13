# ruff: noqa: F811
"""Manual commands never touch DP1/DP2 or use LocalTuya's retrying exchange."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ServiceValidationError

from tests.test_integration import DEVICE, custom, feeder  # noqa: F401

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def capable(hass, sensor, device):
    local = hass.config_entries.async_get_entry(sensor.entry.data["localtuya_entry_id"])
    config = dict(local.data["devices"][DEVICE])
    config.update(
        model="M&C Smart Feeder Titan WIFI",
        protocol_version="3.5",
        dps_strings=[
            f"{dp} ( code: {code} , value: 0 )"
            for dp, code in [
                (1, "meal_plan"),
                (2, "quick_feed"),
                (3, "manual_feed"),
                (4, "feed_state"),
                (13, "fault"),
                (14, "feed_report"),
                (101, "power_mode"),
            ]
        ],
        entities=[
            {
                "id": "3",
                "platform": "number",
                "min_value": 1.0,
                "max_value": 60.0,
                "scaling": 1.0,
                "step_size": 1.0,
            }
        ],
    )
    hass.config_entries.async_update_entry(local, data={"devices": {DEVICE: config}})
    device._status = {"4": "standby", "13": 0, "101": "strong_power"}
    device._pending_status = {}
    device._interface = SimpleNamespace(
        is_connected=True,
        version=3.5,
        local_key=b"session",
        real_local_key=b"original",
        _generate_payload=Mock(side_effect=lambda cmd, dps: (cmd, dps)),
        exchange_quick=AsyncMock(),
    )
    sensor.feed_confirm_timeout = 0.06
    sensor.feed_guard_delay = 0.001
    return device._interface


def received(hass, dp, value, device=DEVICE):
    hass.bus.async_fire(
        "localtuya_device_dp_triggered",
        {"device_id": device, "dp": str(dp), "value": value},
    )


async def test_feed_one_command_with_genuine_reports(hass, feeder):
    sensor, _, transport, device = feeder
    wire = capable(hass, sensor, device)

    async def send(payload, recv_retries):
        assert payload == (7, {"3": 5}) and recv_retries == 0
        received(hass, 4, "feeding")
        received(hass, 14, 5)

    wire.exchange_quick.side_effect = send
    result = await hass.services.async_call(
        "tuya_feeder",
        "feed",
        {"entity_id": sensor.entity_id, "portions": 5},
        blocking=True,
        return_response=True,
    )
    assert result["state"] == "reported" and result["reported"] is True
    wire.exchange_quick.assert_awaited_once()
    transport.assert_not_called()
    assert sensor.extra_state_attributes["feeding_reported_at"]


@pytest.mark.parametrize("portions", [True, False, 1.0, "3", 0, 61, None])
async def test_feed_rejects_invalid_without_command(hass, feeder, portions):
    sensor, _, _, device = feeder
    wire = capable(hass, sensor, device)
    with pytest.raises(ServiceValidationError):
        await sensor.feed(portions)
    wire.exchange_quick.assert_not_called()


async def test_feed_ack_without_reports_unknown_blocks_retry(hass, feeder):
    sensor, _, _, device = feeder
    wire = capable(hass, sensor, device)
    result = await sensor.feed(3)
    assert result["state"] == "unknown" and not result["reported"]
    assert not sensor.extra_state_attributes["supports_manual_feed"]
    with pytest.raises(ServiceValidationError):
        await sensor.feed(3)
    wire.exchange_quick.assert_awaited_once()


async def test_feed_busy_rejected_not_queued(hass, feeder):
    sensor, _, _, device = feeder
    wire = capable(hass, sensor, device)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()

    wire.exchange_quick.side_effect = send
    task = asyncio.create_task(sensor.feed(3))
    await entered.wait()
    with pytest.raises(ServiceValidationError):
        await sensor.feed(3)
    release.set()
    await task
    wire.exchange_quick.assert_awaited_once()


@pytest.mark.parametrize(
    "change", [{"13": 1}, {"13": False}, {"4": "feeding"}, {"101": "weak_power"}]
)
async def test_feed_safety_cache_gate(hass, feeder, change):
    sensor, _, _, device = feeder
    wire = capable(hass, sensor, device)
    device._status.update(change)
    with pytest.raises(ServiceValidationError):
        await sensor.feed(3)
    wire.exchange_quick.assert_not_called()
