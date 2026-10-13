"""UI selection of an existing LocalTuya transport; no credentials requested."""

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .transport import configured_devices


class FeederConfigFlow(config_entries.ConfigFlow, domain="tuya_feeder"):
    """Select a configured feeder; no connection or device writes in this flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return FeederOptionsFlow()

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


class FeederOptionsFlow(config_entries.OptionsFlow):
    """Explicit source whitelist, not automatic ingestion or a device query."""

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            source = user_input.get("historical_snapshot_entity_id")
            state = self.hass.states.get(source) if source else None
            if source and (
                state is None
                or not source.startswith("sensor.")
                or state.attributes.get("source") != "LocalTuya LAN DP1 report"
                or state.attributes.get("read_only") not in (True, "True")
            ):
                errors["base"] = "invalid_snapshot_source"
            else:
                return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "historical_snapshot_entity_id"
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", multiple=False)
                    )
                }
            ),
        )
