"""UI selection of an existing LocalTuya transport; no credentials requested."""

import voluptuous as vol
from homeassistant import config_entries

from .transport import configured_devices


class FeederConfigFlow(config_entries.ConfigFlow, domain="tuya_feeder"):
    """Select a configured feeder; no connection or device writes in this flow."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        devices = configured_devices(self.hass)
        if not devices:
            return self.async_abort(reason="no_devices")
        errors = {}
        if user_input is not None:
            device_id = user_input.get("device_id")
            if device_id not in devices:
                errors["base"] = "device_missing"
            else:
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()
                local_entry_id, name = devices[device_id]
                return self.async_create_entry(
                    title=name,
                    data={"device_id": device_id, "localtuya_entry_id": local_entry_id},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("device_id"): vol.In(
                        {key: value[1] for key, value in devices.items()}
                    )
                }
            ),
            errors=errors,
        )
