"""Real Home Assistant harness: all device writes are intercepted, sockets forbidden."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
)

from custom_components.tuya_feeder.plan import DAYS, encode

DEVICE = "synthetic_feeder"
BASE = [{"days": list(DAYS), "time": "03:21", "portions": 2, "enabled": False}]
CHANGED = [{**BASE[0], "portions": 3}]


@pytest.fixture(autouse=True)
def custom(enable_custom_integrations):
    yield


@pytest.fixture
async def feeder(hass):
    mock_integration(
        hass,
        MockModule(
            "localtuya",
            async_setup=AsyncMock(return_value=True),
            async_setup_entry=AsyncMock(return_value=True),
        ),
    )
    local = MockConfigEntry(
        domain="localtuya",
        state=ConfigEntryState.LOADED,
        data={
            "devices": {
                DEVICE: {"friendly_name": "Synthetic feeder", "host": "synthetic_host"}
            }
        },
    )
    local.add_to_hass(hass)
    device = SimpleNamespace(is_write_only=False, connected=True)
    hass.data["localtuya"] = {
        local.entry_id: SimpleNamespace(devices={"synthetic_host": device})
    }
    transport = AsyncMock()
    hass.services.async_register("localtuya", "set_dp", transport)
    entry = MockConfigEntry(
        domain="tuya_feeder",
        title="Synthetic feeder",
        data={"device_id": DEVICE, "localtuya_entry_id": local.entry_id},
        unique_id=DEVICE,
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.tuya_feeder.transport.async_get_integration",
        AsyncMock(
            return_value=SimpleNamespace(
                manifest={
                    "version": "2026.07.0",
                    "documentation": "https://github.com/xZetsubou/hass-localtuya/",
                }
            )
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    sensor = hass.data["tuya_feeder"][entry.entry_id]
    sensor.confirm_timeout = 0.02
    return sensor, entry, transport, device


async def report(
    hass, meals=BASE, device=DEVICE, event="localtuya_device_dp_triggered", raw=None
):
    hass.bus.async_fire(
        event,
        {
            "device_id": device,
            "dp": "1",
            "value": encode(meals) if raw is None else raw,
        },
    )
    await hass.async_block_till_done()


async def write(hass, sensor, meals=CHANGED, revision=None):
    return await hass.services.async_call(
        "tuya_feeder",
        "set_schedule",
        {
            "entity_id": sensor.entity_id,
            "expected_revision": sensor.revision if revision is None else revision,
            "meals": meals,
        },
        blocking=True,
        return_response=True,
    )


async def test_unknown_wrong_device_and_optimistic(hass, feeder):
    sensor, _, transport, _ = feeder
    assert sensor.native_value is None
    with pytest.raises(HomeAssistantError):
        await write(hass, sensor)
    await report(hass, device="another_synthetic_device")
    await report(hass, event="localtuya_status_update")
    assert sensor.native_value is None
    transport.assert_not_called()
    await report(hass)
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes["meals"] == BASE
    assert sensor.extra_state_attributes["last_received_at"]


async def test_confirmed_exact_dp1_only(hass, feeder):
    sensor, _, transport, _ = feeder
    await report(hass)

    async def receive(call):
        assert call.data == {"device_id": DEVICE, "dp": 1, "value": encode(CHANGED)}
        hass.bus.async_fire(
            "localtuya_device_dp_triggered",
            {"device_id": DEVICE, "dp": "1", "value": call.data["value"]},
        )

    transport.side_effect = receive
    result = await write(hass, sensor)
    assert result == {"confirmed": True, "revision": 2, "error": None}
    assert sensor.extra_state_attributes["meals"] == CHANGED
    assert not sensor.extra_state_attributes["pending"]
    transport.assert_called_once()


async def test_timeout_no_retry_and_no_optimistic_confirmation(hass, feeder):
    sensor, _, transport, _ = feeder
    await report(hass)

    async def optimistic(call):
        hass.bus.async_fire(
            "localtuya_status_update",
            {
                "device_id": DEVICE,
                "old_status": {"1": encode(BASE)},
                "new_status": {"1": call.data["value"]},
            },
        )

    transport.side_effect = optimistic
    result = await write(hass, sensor)
    assert not result["confirmed"]
    assert result["error"] == "unconfirmed_timeout"
    assert sensor.extra_state_attributes["meals"] == BASE
    with pytest.raises(HomeAssistantError):
        await write(hass, sensor)
    transport.assert_called_once()
    await report(hass, CHANGED)
    assert sensor.extra_state_attributes["error"] is None


async def test_stale_revision_unchanged_and_write_only(hass, feeder):
    sensor, _, transport, device = feeder
    await report(hass)
    with pytest.raises(HomeAssistantError):
        await write(hass, sensor, revision=0)
    result = await write(hass, sensor, meals=BASE)
    assert result["error"] == "unchanged_no_write"
    transport.assert_not_called()
    device.is_write_only = True
    with pytest.raises(HomeAssistantError):
        await write(hass, sensor)
    await report(hass, CHANGED)
    assert sensor.extra_state_attributes["meals"] == BASE


async def test_concurrent_writer_rejected(hass, feeder):
    sensor, _, transport, _ = feeder
    await report(hass)
    import asyncio

    first = asyncio.create_task(write(hass, sensor))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    with pytest.raises(HomeAssistantError):
        await write(hass, sensor)
    assert not (await first)["confirmed"]
    transport.assert_called_once()


async def test_malformed_snapshot_fail_closed(hass, feeder):
    sensor, _, transport, _ = feeder
    await report(hass)
    await report(hass, raw="AA==")
    assert not sensor.extra_state_attributes["supports_schedule_write"]
    with pytest.raises(HomeAssistantError):
        await write(hass, sensor)
    transport.assert_not_called()


async def test_storage_and_unload(hass, feeder):
    from custom_components.tuya_feeder import async_unload_entry

    sensor, entry, transport, _ = feeder
    await report(hass)
    stored = await sensor.store.async_load()
    assert stored["raw"] == encode(BASE)
    assert stored["revision"] == 1
    assert await async_unload_entry(hass, entry)
    await report(hass, CHANGED)
    assert sensor.revision == 1
    transport.assert_not_called()


async def test_optional_response(hass, feeder):
    assert (
        hass.services.async_services()["tuya_feeder"]["set_schedule"].supports_response
        == SupportsResponse.OPTIONAL
    )


async def test_config_flow_existing_selection_duplicates_and_invalid(hass, feeder):
    from custom_components.tuya_feeder.config_flow import FeederConfigFlow

    flow = FeederConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow.handler = "tuya_feeder"
    result = await flow.async_step_user()
    assert result["type"] == "form"
    result = await flow.async_step_user({"device_id": "missing"})
    assert result["errors"] == {"base": "device_missing"}
    from homeassistant.data_entry_flow import AbortFlow

    with pytest.raises(AbortFlow) as abort:
        await flow.async_step_user({"device_id": DEVICE})
    assert abort.value.reason == "already_configured"


async def test_config_flow_create_and_empty(hass):
    from custom_components.tuya_feeder.config_flow import FeederConfigFlow

    flow = FeederConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow.handler = "tuya_feeder"
    assert (await flow.async_step_user())["reason"] == "no_devices"
    local = MockConfigEntry(
        domain="localtuya",
        data={"devices": {DEVICE: {"friendly_name": "Synthetic feeder"}}},
    )
    local.add_to_hass(hass)
    result = await flow.async_step_user({"device_id": DEVICE})
    assert result["type"] == "create_entry"
    assert result["data"] == {"device_id": DEVICE, "localtuya_entry_id": local.entry_id}


async def test_unexpected_transport_exception_is_explicit(hass, feeder):
    sensor, _, transport, _ = feeder
    await report(hass)
    transport.side_effect = RuntimeError("synthetic failure")
    result = await write(hass, sensor)
    assert result["error"] == "unconfirmed_transport_error"
    assert not result["confirmed"]
    assert not sensor.extra_state_attributes["supports_schedule_write"]
    transport.assert_called_once()


async def test_restore_after_real_config_entry_reload(hass, feeder):
    sensor, entry, transport, _ = feeder
    await report(hass)
    timestamp = sensor.last_received_at
    await write(hass, sensor)  # Intercepted, deliberately unconfirmed.
    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch(
        "custom_components.tuya_feeder.transport.async_get_integration",
        AsyncMock(
            return_value=SimpleNamespace(
                manifest={
                    "version": "2026.07.0",
                    "documentation": "https://github.com/xZetsubou/hass-localtuya/",
                }
            )
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    restored = hass.data["tuya_feeder"][entry.entry_id]
    assert restored.meals == BASE
    assert restored.last_received_at == timestamp
    assert restored.revision == 1
    assert restored.error == "unconfirmed_timeout"
    assert not restored.extra_state_attributes["supports_schedule_write"]
    transport.assert_called_once()
    await report(hass)
    assert restored.extra_state_attributes["supports_schedule_write"]


async def test_restored_valid_snapshot_requires_new_local_receipt(hass, feeder):
    from custom_components.tuya_feeder.sensor import MealPlanSensor

    sensor, entry, transport, _ = feeder
    await report(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    restored = MealPlanSensor(hass, entry, True)
    await restored.start()
    assert not restored.extra_state_attributes["supports_schedule_write"]
    assert restored.extra_state_attributes["schedule_valid"]
    assert restored.extra_state_attributes["snapshot_source"] == "restored"
    with pytest.raises(HomeAssistantError):
        await restored.set_schedule(restored.revision, CHANGED)
    transport.assert_not_called()
    await report(hass)
    assert restored.extra_state_attributes["snapshot_source"] == "received"
    assert restored.extra_state_attributes["supports_schedule_write"]
    await restored.stop()


async def test_wrong_dp_incompatible_runtime_and_disconnected(hass, feeder):
    sensor, _, transport, device = feeder
    hass.bus.async_fire(
        "localtuya_device_dp_triggered",
        {"device_id": DEVICE, "dp": "2", "value": encode(BASE)},
    )
    await hass.async_block_till_done()
    assert sensor.meals is None
    sensor.audited = False
    await report(hass)
    assert sensor.meals is None
    sensor.audited = True
    await report(hass)
    device.connected = False
    with pytest.raises(HomeAssistantError):
        await write(hass, sensor)
    transport.assert_not_called()


@pytest.mark.parametrize("revision", [True, "1", 1.0, None])
async def test_strict_revision_types(hass, feeder, revision):
    sensor, _, transport, _ = feeder
    await report(hass)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "tuya_feeder",
            "set_schedule",
            {
                "entity_id": sensor.entity_id,
                "expected_revision": revision,
                "meals": CHANGED,
            },
            blocking=True,
        )
    transport.assert_not_called()


async def test_wrong_entity_and_invalid_plan_do_not_write(hass, feeder):
    sensor, _, transport, _ = feeder
    await report(hass)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "tuya_feeder",
            "set_schedule",
            {
                "entity_id": "sensor.not_a_feeder",
                "expected_revision": 1,
                "meals": CHANGED,
            },
            blocking=True,
        )
    with pytest.raises(HomeAssistantError):
        await write(hass, sensor, meals=[{**BASE[0], "portions": True}])
    transport.assert_not_called()


async def test_nonresponse_timeout_raises(hass, feeder):
    sensor, _, transport, _ = feeder
    await report(hass)
    with pytest.raises(HomeAssistantError, match="unconfirmed_timeout"):
        await hass.services.async_call(
            "tuya_feeder",
            "set_schedule",
            {"entity_id": sensor.entity_id, "expected_revision": 1, "meals": CHANGED},
            blocking=True,
        )
    transport.assert_called_once()


async def test_every_record_preserved_in_complete_replacement(hass, feeder):
    sensor, _, transport, _ = feeder
    original = [
        {**BASE[0], "time": f"{i:02d}:21", "portions": i + 1, "enabled": bool(i % 2)}
        for i in range(10)
    ]
    await report(hass, original)
    proposed = [dict(meal) for meal in original]
    proposed[4] = {**proposed[4], "portions": 60}

    async def receive(call):
        assert call.data["value"] == encode(proposed)
        hass.bus.async_fire(
            "localtuya_device_dp_triggered",
            {"device_id": DEVICE, "dp": "1", "value": call.data["value"]},
        )

    transport.side_effect = receive
    assert (await write(hass, sensor, meals=proposed))["confirmed"]
    assert sensor.meals == proposed
    assert original[:4] == sensor.meals[:4]
    assert original[5:] == sensor.meals[5:]


async def test_report_while_reserving_aborts_without_send(hass, feeder):
    sensor, _, transport, _ = feeder
    await report(hass)
    original_save = sensor.save
    first = True

    async def saving():
        nonlocal first
        if first:
            first = False
            await report(hass, CHANGED)
        await original_save()

    with patch.object(sensor, "save", saving):
        result = await write(hass, sensor)
    assert result["error"] == "changed_before_send_no_write"
    assert not result["confirmed"]
    transport.assert_not_called()


async def test_invalid_storage_cannot_become_writable(hass, feeder):
    from custom_components.tuya_feeder.sensor import MealPlanSensor

    sensor, entry, transport, _ = feeder
    await sensor.store.async_save(
        {"raw": "AA==", "revision": 1, "last_received_at": "bad"}
    )
    fresh = MealPlanSensor(hass, entry, True)
    await fresh.start()
    assert fresh.meals is None
    assert fresh.error == "invalid_stored_snapshot"
    await fresh.stop()
    transport.assert_not_called()


async def test_malformed_report_metadata_survives_restore(hass, feeder):
    from custom_components.tuya_feeder.sensor import MealPlanSensor

    sensor, entry, transport, _ = feeder
    await report(hass)
    await report(hass, raw="AA==")
    timestamp = sensor.last_received_at
    assert sensor.revision == 2
    assert await hass.config_entries.async_unload(entry.entry_id)
    restored = MealPlanSensor(hass, entry, True)
    await restored.start()
    assert restored.revision == 2
    assert restored.last_received_at == timestamp
    assert restored.error == "unsupported_or_malformed_report"
    assert restored.meals is None
    await restored.stop()
    transport.assert_not_called()


async def test_config_entry_only_schema_warns_on_unsupported_yaml(caplog):
    from custom_components.tuya_feeder import CONFIG_SCHEMA

    assert CONFIG_SCHEMA({}) == {}
    CONFIG_SCHEMA({"tuya_feeder": {"device_id": "synthetic_feeder"}})
    assert "does not support YAML setup" in caplog.text


async def test_transport_audit_requires_exact_version_and_origin(hass):
    from custom_components.tuya_feeder.transport import audited_transport

    for manifest in [
        {},
        {
            "version": "other",
            "documentation": "https://github.com/xZetsubou/hass-localtuya/",
        },
        {"version": "2026.07.0", "documentation": "https://example.invalid/"},
    ]:
        with patch(
            "custom_components.tuya_feeder.transport.async_get_integration",
            AsyncMock(return_value=SimpleNamespace(manifest=manifest)),
        ):
            assert not await audited_transport(hass)


@pytest.mark.parametrize("write_only_value", [None, ""])
async def test_audited_wifi_falsey_write_only_property(hass, feeder, write_only_value):
    sensor, _, transport, device = feeder
    device.is_write_only = write_only_value
    await report(hass)
    assert sensor.meals == BASE
    assert sensor.extra_state_attributes["supports_schedule_write"]
    transport.assert_not_called()


async def test_missing_write_only_property_fails_closed(hass, feeder):
    sensor, _, transport, device = feeder
    del device.is_write_only
    await report(hass)
    assert sensor.meals is None
    transport.assert_not_called()
