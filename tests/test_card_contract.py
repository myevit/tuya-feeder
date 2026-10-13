"""Runtime HA-produced states, real Chromium, intercepted synthetic-only transport.

Set TUYA_FEEDER_CARD_DIR to a built sibling card checkout to exercise this gate.
No static invented HA state fixture is accepted: state comes from hass.states.
"""

# pytest fixture imports intentionally share names with test arguments.
# ruff: noqa: F811
import asyncio
import json
import os
from pathlib import Path

import pytest
from homeassistant.exceptions import HomeAssistantError
from test_integration import CHANGED, DEVICE, custom, feeder, report  # noqa: F401, F811

from custom_components.tuya_feeder.plan import encode

CARD = os.environ.get("TUYA_FEEDER_CARD_DIR")
pytestmark = pytest.mark.skipif(
    not CARD, reason="Cross-package gate requires built card checkout"
)


@pytest.fixture
async def browser_bridge():
    process = await asyncio.create_subprocess_exec(
        "node",
        str(Path(CARD) / "tests/integration_bridge.mjs"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    async def command(action, **data):
        process.stdin.write((json.dumps({"action": action, **data}) + "\n").encode())
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 15)
        assert line, "Chromium bridge exited unexpectedly"
        result = json.loads(line)
        assert result["ok"], result
        return result["calls"]

    yield command
    process.stdin.write(b'{"action":"close"}\n')
    await process.stdin.drain()
    await asyncio.wait_for(process.wait(), 15)
    assert process.returncode == 0


def state(hass, sensor):
    # Actual integration-produced HA entity state; deliberately synthetic device.
    actual = hass.states.get(sensor.entity_id)
    return {
        "entity_id": actual.entity_id,
        "state": actual.state,
        "attributes": dict(actual.attributes),
    }


async def test_actual_card_save_backend_report_contract(hass, feeder, browser_bridge):
    sensor, _, transport, _ = feeder
    sensor.confirm_timeout = 10
    await report(hass)
    initial = state(hass, sensor)
    assert initial["attributes"]["observation_kind"] != "confirmed"
    await browser_bridge("init", state=initial)
    await browser_bridge("editable")
    calls = await browser_bridge("save")
    assert calls == [
        [
            "tuya_feeder",
            "set_schedule",
            {
                "entity_id": sensor.entity_id,
                "expected_revision": 1,
                "meals": CHANGED,
            },
        ]
    ]
    await browser_bridge("busy")  # Empty frontend ACK never confirms.

    async def receive(call):
        assert call.data == {"device_id": DEVICE, "dp": 1, "value": encode(CHANGED)}
        await browser_bridge("state", state=state(hass, sensor))
        await browser_bridge("busy")  # Actual pending reservation.
        await report(hass, CHANGED)
        await browser_bridge("state", state=state(hass, sensor))
        await browser_bridge(
            "busy"
        )  # Matching receipt remains pending until backend finishes.

    transport.side_effect = receive
    domain, service, payload = calls[0]
    result = await hass.services.async_call(
        domain, service, payload, blocking=True, return_response=True
    )
    assert result == {"confirmed": True, "revision": 2, "error": None}
    final = state(hass, sensor)
    assert (
        final["attributes"]["last_received_at"]
        != initial["attributes"]["last_received_at"]
    )
    await browser_bridge("state", state=final)
    await browser_bridge("confirmed")
    assert len(await browser_bridge("editable")) == 1
    transport.assert_called_once()


@pytest.mark.parametrize(
    "case",
    ["unknown", "restored", "corrupt", "mixed_days", "unsupported", "empty_unknown"],
)
async def test_actual_states_fail_closed(hass, feeder, browser_bridge, case):
    from custom_components.tuya_feeder.sensor import MealPlanSensor

    sensor, entry, transport, device = feeder
    if case not in ("unknown", "empty_unknown"):
        await report(hass)
    if case == "restored":
        assert await hass.config_entries.async_unload(entry.entry_id)
        sensor = MealPlanSensor(hass, entry, True)
        await sensor.start()
        sensor.entity_id = "sensor.synthetic_restored"
        sensor._publish()
    elif case == "corrupt":
        await report(hass, raw="AA==")
    elif case == "mixed_days":
        import base64

        await report(hass, raw=base64.b64encode(bytes([1, 3, 21, 2, 0])).decode())
    elif case == "unsupported":
        device.connected = False
        sensor._publish()
    await browser_bridge("init", state=state(hass, sensor))
    assert await browser_bridge("readonly") == []
    with pytest.raises(HomeAssistantError):
        await sensor.set_schedule(sensor.revision, CHANGED)
    transport.assert_not_called()
    if case == "restored":
        await sensor.stop()


async def test_actual_timeout_preserves_draft_and_no_retry(
    hass, feeder, browser_bridge
):
    sensor, _, transport, _ = feeder
    await report(hass)
    await browser_bridge("init", state=state(hass, sensor))
    calls = await browser_bridge("save")
    result = await hass.services.async_call(
        *calls[0][:2], calls[0][2], blocking=True, return_response=True
    )
    assert result["error"] == "unconfirmed_timeout"
    await browser_bridge("state", state=state(hass, sensor))
    await browser_bridge("error", error="unconfirmed_timeout")
    assert len(await browser_bridge("readonly")) == 1
    transport.assert_called_once()


async def test_actual_edit_conflict(hass, feeder, browser_bridge):
    sensor, _, transport, _ = feeder
    await report(hass)
    await browser_bridge("init", state=state(hass, sensor))
    await browser_bridge("edit")
    await report(hass, CHANGED)
    await browser_bridge("state", state=state(hass, sensor))
    assert await browser_bridge("conflict") == []
    transport.assert_not_called()
