"""Tuya feeder schedules over an existing LocalTuya connection."""

import inspect

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .sensor import MealPlanSensor
from .transport import audited_transport, configured_devices

DOMAIN = "tuya_feeder"
PLATFORMS = [Platform.SENSOR]


async def async_setup(hass, config):
    """Register the action even if no entries are loaded."""
    hass.data.setdefault(DOMAIN, {})

    async def set_schedule(call):
        entity_id = call.data["entity_id"]
        sensor = next(
            (
                sensor
                for sensor in hass.data[DOMAIN].values()
                if sensor.entity_id == entity_id
            ),
            None,
        )
        if sensor is None:
            raise ServiceValidationError("Select a loaded Tuya Feeder meal-plan sensor")
        response = await sensor.set_schedule(
            call.data["expected_revision"], call.data["meals"]
        )
        if getattr(call, "return_response", False):
            return response
        if response["error"] not in (None, "unchanged_no_write"):
            raise ServiceValidationError(response["error"])
        return None

    kwargs = {}
    if (
        "supports_response"
        in inspect.signature(hass.services.async_register).parameters
    ):
        kwargs["supports_response"] = SupportsResponse.OPTIONAL
    hass.services.async_register(
        DOMAIN,
        "set_schedule",
        set_schedule,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_id,
                vol.Required("expected_revision"): object,
                vol.Required("meals"): object,
            }
        ),
        **kwargs,
    )
    return True


async def async_setup_entry(hass, entry):
    """Create no sockets and read no cloud data."""
    devices = configured_devices(hass)
    if (
        entry.data["device_id"] not in devices
        or devices[entry.data["device_id"]][0] != entry.data["localtuya_entry_id"]
    ):
        return False
    sensor = MealPlanSensor(hass, entry, await audited_transport(hass))
    await sensor.start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = sensor
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await sensor.stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    return True


async def async_unload_entry(hass, entry):
    """Remove our subscription/entity, leave LocalTuya and feeder untouched."""
    sensor = hass.data[DOMAIN].get(entry.entry_id)
    if sensor is not None and sensor.pending:
        return False
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        if sensor is not None:
            await sensor.stop()
            hass.data[DOMAIN].pop(entry.entry_id, None)
        return True
    return False


async def async_remove_entry(hass, entry):
    """Explicit removal deletes only our private snapshot."""
    from homeassistant.helpers.storage import Store

    await Store(hass, 1, f"tuya_feeder.{entry.entry_id}").async_remove()
