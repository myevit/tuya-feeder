"""Reuse only the audited LocalTuya event/service surface, never its cache."""

from homeassistant.loader import async_get_integration

AUDITED_VERSION = "2026.07.0"
AUDITED_DOCS = "https://github.com/xZetsubou/hass-localtuya/"


def configured_devices(hass):
    """Return existing LocalTuya devices without copying keys or addresses."""
    devices = {}
    for entry in hass.config_entries.async_entries("localtuya"):
        for device_id, config in entry.data.get("devices", {}).items():
            devices[device_id] = (entry.entry_id, config.get("friendly_name", "Feeder"))
    return devices


def runtime_device(hass, entry):
    """Resolve audited runtime (never expose the host in entity attributes)."""
    local_entry = hass.config_entries.async_get_entry(entry.data["localtuya_entry_id"])
    if local_entry is None or local_entry.domain != "localtuya":
        return None
    config = local_entry.data.get("devices", {}).get(entry.data["device_id"])
    if config is None:
        return None
    host = config.get("host")
    if config.get("node_id"):
        return None  # Direct Wi-Fi FT-11W only; no gateway assumptions.
    runtime = hass.data.get("localtuya", {}).get(local_entry.entry_id)
    return getattr(runtime, "devices", {}).get(host)


async def audited_transport(hass):
    """Fail closed on forks/versions whose optimistic event behavior is unaudited."""
    integration = await async_get_integration(hass, "localtuya")
    return (
        integration.manifest.get("version") == AUDITED_VERSION
        and integration.manifest.get("documentation") == AUDITED_DOCS
    )


def genuine_reports_allowed(hass, entry, audited):
    """Write-only runtimes synthesize status_updated; their events are unsafe."""
    device = runtime_device(hass, entry)
    # Audited property is `is_subdevice and ...`; direct Wi-Fi returns
    # None or an empty node ID, not necessarily the literal False.
    write_only = getattr(device, "is_write_only", True)
    return (
        audited
        and device is not None
        and (write_only is False or write_only is None or write_only == "")
    )


def can_send(hass, entry, audited):
    """Only a connected nonoptimistic runtime with the installed service."""
    device = runtime_device(hass, entry)
    return (
        genuine_reports_allowed(hass, entry, audited)
        and getattr(device, "connected", False)
        and hass.services.has_service("localtuya", "set_dp")
    )
