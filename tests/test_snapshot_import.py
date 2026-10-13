"""Historical import never dispatches a device report or write."""

# pytest fixture imports intentionally share names with arguments.
# ruff: noqa: F811
import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from test_integration import BASE, CHANGED, custom, feeder, report  # noqa: F401

from custom_components.tuya_feeder.plan import encode

SOURCE = "sensor.synthetic_passive_plan"
STAMP = "2026-01-01T00:00:00+00:00"


async def import_saved(hass, sensor, entry, **attrs):
    hass.config_entries.async_update_entry(
        entry, options={"historical_snapshot_entity_id": SOURCE}
    )
    hass.states.async_set(
        SOURCE,
        "1",
        {
            "raw_plan": encode(BASE),
            "received_at": STAMP,
            "source": "LocalTuya LAN DP1 report",
            "read_only": "True",
            **attrs,
        },
    )
    await hass.services.async_call(
        "tuya_feeder",
        "import_snapshot",
        {"entity_id": sensor.entity_id, "source_entity_id": SOURCE},
        blocking=True,
    )


async def test_import_preserves_original_receipt_and_never_sends(hass, feeder):
    sensor, entry, transport, _ = feeder
    await import_saved(hass, sensor, entry)
    assert sensor.meals == BASE
    assert sensor.snapshot_source == "restored"
    assert sensor.last_received_at == STAMP
    assert sensor.extra_state_attributes["requires_restored_snapshot_acknowledgement"]
    assert not sensor.extra_state_attributes["supports_schedule_write"]
    assert sensor.extra_state_attributes["supports_restored_schedule_write"]
    stored = await sensor.store.async_load()
    assert stored["imported_from"] == SOURCE
    assert stored["last_received_at"] == STAMP
    transport.assert_not_called()


@pytest.mark.parametrize(
    "attrs",
    [
        {"raw_plan": None},
        {"raw_plan": "AA=="},
        {"received_at": None},
        {"received_at": "bad"},
        {"received_at": "2026-01-01T00:00:00"},
        {"source": "cloud shadow"},
        {"read_only": False},
    ],
)
async def test_invalid_import_fail_closed(hass, feeder, attrs):
    sensor, entry, transport, _ = feeder
    with pytest.raises(HomeAssistantError):
        await import_saved(hass, sensor, entry, **attrs)
    assert sensor.meals is None
    transport.assert_not_called()


async def test_unselected_source_is_rejected(hass, feeder):
    sensor, _, transport, _ = feeder
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "tuya_feeder",
            "import_snapshot",
            {"entity_id": sensor.entity_id, "source_entity_id": SOURCE},
            blocking=True,
        )
    transport.assert_not_called()


async def test_import_cannot_replace_live_or_uncertain_snapshot(hass, feeder):
    sensor, entry, transport, _ = feeder
    await report(hass)
    with pytest.raises(HomeAssistantError):
        await import_saved(hass, sensor, entry)
    transport.assert_not_called()


async def test_imported_write_requires_strict_ack_and_new_receipt(hass, feeder):
    sensor, entry, transport, _ = feeder
    await import_saved(hass, sensor, entry)
    payload = {
        "entity_id": sensor.entity_id,
        "expected_revision": sensor.revision,
        "meals": CHANGED,
    }
    for ack in [None, False, "true", 1]:
        with pytest.raises((HomeAssistantError, vol.Invalid)):
            await hass.services.async_call(
                "tuya_feeder",
                "set_schedule",
                {**payload, "acknowledge_restored_snapshot": ack},
                blocking=True,
            )
    transport.assert_not_called()

    async def receive(call):
        await report(hass, CHANGED)

    transport.side_effect = receive
    result = await hass.services.async_call(
        "tuya_feeder",
        "set_schedule",
        {**payload, "acknowledge_restored_snapshot": True},
        blocking=True,
        return_response=True,
    )
    assert result["confirmed"]
    assert sensor.snapshot_source == "received"
    assert sensor.last_received_at != STAMP
    transport.assert_called_once()


async def test_options_selects_existing_passive_sensor(hass, feeder):
    from homeassistant.data_entry_flow import FlowResultType

    _, entry, transport, _ = feeder
    hass.states.async_set(
        SOURCE,
        "1",
        {
            "raw_plan": encode(BASE),
            "received_at": STAMP,
            "source": "LocalTuya LAN DP1 report",
            "read_only": "True",
        },
    )
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] == FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"historical_snapshot_entity_id": SOURCE}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["historical_snapshot_entity_id"] == SOURCE
    transport.assert_not_called()


async def test_import_restores_as_historical_not_receipt(hass, feeder):
    from custom_components.tuya_feeder.sensor import MealPlanSensor

    sensor, entry, transport, _ = feeder
    await import_saved(hass, sensor, entry)
    await sensor.stop()
    restored = MealPlanSensor(hass, entry, True)
    await restored.start()
    assert restored.snapshot_source == "restored"
    assert restored.last_received_at == STAMP
    assert restored.imported_from == SOURCE
    assert restored.meals == BASE
    transport.assert_not_called()
    await restored.stop()


async def test_non_admin_cannot_import(hass, feeder, hass_read_only_user):
    from homeassistant.core import Context

    sensor, entry, transport, _ = feeder
    hass.config_entries.async_update_entry(
        entry, options={"historical_snapshot_entity_id": SOURCE}
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "tuya_feeder",
            "import_snapshot",
            {"entity_id": sensor.entity_id, "source_entity_id": SOURCE},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    transport.assert_not_called()


@pytest.mark.parametrize("unsafe", ["pending", "uncertain", "error", "disconnected"])
async def test_imported_ack_cannot_bypass_unsafe_gate(hass, feeder, unsafe):
    sensor, entry, transport, device = feeder
    await import_saved(hass, sensor, entry)
    if unsafe == "disconnected":
        device.connected = False
    else:
        setattr(sensor, unsafe, True)
    with pytest.raises(HomeAssistantError):
        await sensor.set_schedule(sensor.revision, CHANGED, True)
    transport.assert_not_called()
    sensor.pending = False
