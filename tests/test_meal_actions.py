# ruff: noqa: F811
"""Per-meal actions use the actual HA handler and intercepted LAN writes."""

import asyncio
import base64

import pytest
from homeassistant.exceptions import ServiceValidationError

from tests.test_integration import BASE, DEVICE, custom, feeder, report  # noqa: F401


async def test_update_meal_preserves_other_wire_records(hass, feeder):
    sensor, _, transport, _ = feeder
    original = [{**BASE[0], "time": f"{i:02d}:21", "portions": i + 1} for i in range(3)]
    await report(hass, original)
    proposed = {**original[1], "portions": 60}

    async def echo(call):
        assert call.data["dp"] == 1
        raw = base64.b64decode(call.data["value"])
        before = base64.b64decode(sensor.raw)
        assert raw[:5] == before[:5] and raw[10:] == before[10:]
        hass.bus.async_fire(
            "localtuya_device_dp_triggered",
            {"device_id": DEVICE, "dp": "1", "value": call.data["value"]},
        )

    transport.side_effect = echo
    result = await hass.services.async_call(
        "tuya_feeder",
        "update_meal",
        {
            "entity_id": sensor.entity_id,
            "expected_revision": 1,
            "meal_index": 1,
            "meal": proposed,
        },
        blocking=True,
        return_response=True,
    )
    assert result["confirmed"]
    assert sensor.meals == [original[0], proposed, original[2]]
    transport.assert_called_once()


@pytest.mark.parametrize("action", ["add_meal", "delete_meal"])
async def test_add_delete_preserves_other_records(hass, feeder, action):
    sensor, _, transport, _ = feeder
    original = [{**BASE[0], "time": f"{i:02d}:25"} for i in range(3)]
    await report(hass, original)
    raw_before = base64.b64decode(sensor.raw)

    async def echo(call):
        raw = base64.b64decode(call.data["value"])
        assert call.data["dp"] == 1
        if action == "add_meal":
            assert raw[:15] == raw_before
        else:
            assert raw == raw_before[:5] + raw_before[10:]
        hass.bus.async_fire(
            "localtuya_device_dp_triggered",
            {"device_id": DEVICE, "dp": "1", "value": call.data["value"]},
        )

    transport.side_effect = echo
    data = {"entity_id": sensor.entity_id, "expected_revision": 1}
    if action == "add_meal":
        data["meal"] = {**BASE[0], "time": "17:25"}
    else:
        data["meal_index"] = 1
    result = await hass.services.async_call(
        "tuya_feeder", action, data, blocking=True, return_response=True
    )
    assert result["confirmed"]
    transport.assert_called_once()


@pytest.mark.parametrize("index", [True, False, 1.0, "1", -1, 3])
async def test_bad_index_no_write(hass, feeder, index):
    sensor, _, transport, _ = feeder
    await report(hass, BASE)
    with pytest.raises(ServiceValidationError):
        await sensor.meal_action("update_meal", 1, index, BASE[0])
    transport.assert_not_called()


async def test_stale_index_and_final_delete_no_write(hass, feeder):
    sensor, _, transport, _ = feeder
    await report(hass, [BASE[0]])
    for revision in (True, 1.0, "1", 0):
        with pytest.raises(ServiceValidationError):
            await sensor.meal_action("delete_meal", revision, 0)
    with pytest.raises(ServiceValidationError, match="final meal"):
        await sensor.meal_action("delete_meal", 1, 0)
    transport.assert_not_called()


async def test_meal_max_limits_and_mixed_days_no_write(hass, feeder):
    sensor, _, transport, _ = feeder
    await report(hass, [{**BASE[0], "time": f"{i:02d}:00"} for i in range(10)])
    with pytest.raises(ServiceValidationError):
        await sensor.meal_action("add_meal", 1, meal=BASE[0])
    for meal in (
        {**BASE[0], "days": ["monday"]},
        {**BASE[0], "portions": True},
        {**BASE[0], "portions": 61},
    ):
        with pytest.raises(ServiceValidationError):
            await sensor.meal_action("update_meal", 1, 0, meal)
    transport.assert_not_called()


async def test_restored_meal_requires_scoped_ack_and_new_echo(hass, feeder):
    sensor, _, transport, _ = feeder
    await report(hass, BASE)
    sensor.snapshot_source = "restored"
    edited = {**BASE[0], "portions": 20}
    with pytest.raises(ServiceValidationError):
        await sensor.meal_action("update_meal", 1, 0, edited)
    transport.assert_not_called()

    async def echo(call):
        hass.bus.async_fire(
            "localtuya_device_dp_triggered",
            {"device_id": DEVICE, "dp": "1", "value": call.data["value"]},
        )

    transport.side_effect = echo
    result = await sensor.meal_action("update_meal", 1, 0, edited, True)
    assert result["confirmed"] and result["revision"] == 2


async def test_meal_concurrency_and_unknown_snapshot_fail_closed(hass, feeder):
    sensor, _, transport, _ = feeder
    with pytest.raises(ServiceValidationError):
        await sensor.meal_action("add_meal", 0, meal=BASE[0])
    await report(hass, BASE)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(call):
        entered.set()
        await release.wait()

    transport.side_effect = blocked
    sensor.confirm_timeout = 0.04
    task = asyncio.create_task(
        sensor.meal_action("update_meal", 1, 0, {**BASE[0], "portions": 42})
    )
    await entered.wait()
    with pytest.raises(ServiceValidationError):
        await sensor.meal_action("delete_meal", 1, 0)
    release.set()
    result = await task
    assert result["error"] == "unconfirmed_timeout"
    transport.assert_called_once()
