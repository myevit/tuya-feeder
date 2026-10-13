# ruff: noqa: F811
"""New card actions through actual HA handlers, no network or physical feeding."""

import pytest

from custom_components.tuya_feeder.plan import decode
from tests.test_card_contract import browser_bridge, state  # noqa: F401
from tests.test_feed import capable, received
from tests.test_integration import BASE, custom, feeder, report  # noqa: F401

pytestmark = pytest.mark.skipif(
    __import__("os").environ.get("TUYA_FEEDER_CARD_DIR") is None,
    reason="Requires bundled card",
)


@pytest.mark.parametrize("action", ["add", "delete"])
async def test_card_meal_actions_through_actual_handler(
    hass, feeder, browser_bridge, action
):
    sensor, _, transport, _ = feeder
    sensor.confirm_timeout = 10
    meals = [{**BASE[0], "time": "03:21"}, {**BASE[0], "time": "17:25"}]
    await report(hass, meals)
    await browser_bridge("init", state=state(hass, sensor))
    calls = await browser_bridge(action)
    assert len(calls) == 1 and calls[0][1] == action + "_meal"

    async def echo(call):
        assert call.data["dp"] == 1
        edited = decode(call.data["value"])
        if action == "add":
            assert edited[: len(meals)] == meals
        else:
            assert edited == [meals[1]]
        await report(hass, edited)

    transport.side_effect = echo
    result = await hass.services.async_call(
        *calls[0][:2], calls[0][2], blocking=True, return_response=True
    )
    assert result["confirmed"]
    await browser_bridge("service_result", result=result)
    await browser_bridge("state", state=state(hass, sensor))
    await browser_bridge("confirmed")
    transport.assert_called_once()


async def test_card_feed_through_one_shot_handler(hass, feeder, browser_bridge):
    sensor, _, transport, device = feeder
    wire = capable(hass, sensor, device)
    sensor.feed_confirm_timeout = 10
    await report(hass, BASE)
    sensor._publish()
    await browser_bridge("init", state=state(hass, sensor), manual=True)
    calls = await browser_bridge("feed")
    assert len(calls) == 1 and calls[0][:2] == ["tuya_feeder", "feed"]
    portions = calls[0][2]["portions"]

    async def send(payload, recv_retries):
        assert payload == (7, {"3": portions}) and recv_retries == 0
        received(hass, 4, "feeding")
        await hass.async_block_till_done()
        assert sensor.extra_state_attributes["feeding_state"] == "feeding"
        await browser_bridge("state", state=state(hass, sensor))
        received(hass, 14, portions)

    wire.exchange_quick.side_effect = send
    result = await hass.services.async_call(
        *calls[0][:2], calls[0][2], blocking=True, return_response=True
    )
    assert result["reported"]
    await browser_bridge("service_result", result=result)
    await browser_bridge("state", state=state(hass, sensor))
    transport.assert_not_called()
    wire.exchange_quick.assert_awaited_once()
