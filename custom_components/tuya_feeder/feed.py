"""One-shot manual feeding for the exact audited Titan WIFI mapping.

The shared LocalTuya protocol's exchange() can resend CONTROL after a device
format change. Use exchange_quick(..., 0) instead: one encrypted frame, no ACK
interpreted as delivery and no retry. The connected negotiated session is reused.
"""

import asyncio
import re

from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .transport import can_send, genuine_reports_allowed, runtime_device

MODEL = "M&C Smart Feeder Titan WIFI"
CODES = {
    1: "meal_plan",
    2: "quick_feed",
    3: "manual_feed",
    4: "feed_state",
    13: "fault",
    14: "feed_report",
    101: "power_mode",
}


def manual_capability(sensor):
    """Exact installed model, configured code IDs and unscaled 1..60 schema."""
    hass, entry = sensor.hass, sensor.entry
    local = hass.config_entries.async_get_entry(entry.data["localtuya_entry_id"])
    config = (
        local.data.get("devices", {}).get(entry.data["device_id"], {}) if local else {}
    )
    if config.get("model") != MODEL or str(config.get("protocol_version")) != "3.5":
        return False
    codes = {}
    for line in config.get("dps_strings", []):
        if type(line) is str and (
            match := re.match(r"^(\d+) \( code: ([a-z_]+) ,", line)
        ):
            codes[int(match[1])] = match[2]
    if any(codes.get(dp) != code for dp, code in CODES.items()):
        return False
    return any(
        str(e.get("id")) == "3"
        and e.get("platform") == "number"
        and e.get("min_value") == 1
        and e.get("max_value") == 60
        and e.get("scaling") == 1
        and e.get("step_size") == 1
        and e.get("restore_on_reconnect", False) is False
        for e in config.get("entities", [])
    )


def ready(sensor):
    """Cache is ONLY a conservative precondition, never completion evidence."""
    if not manual_capability(sensor) or not can_send(
        sensor.hass, sensor.entry, sensor.audited
    ):
        return False
    device = runtime_device(sensor.hass, sensor.entry)
    cache = getattr(device, "_status", {})
    interface = getattr(device, "_interface", None)
    return (
        cache.get("4") in ("standby", "done")
        and type(cache.get("13")) is int
        and cache["13"] == 0
        and cache.get("101") == "strong_power"
        and not getattr(device, "_pending_status", {"unknown": True})
        and interface is not None
        and getattr(interface, "is_connected", False)
        and getattr(interface, "version", None) == 3.5
        and getattr(interface, "local_key", None)
        != getattr(interface, "real_local_key", None)
        and callable(getattr(interface, "_generate_payload", None))
        and callable(getattr(interface, "exchange_quick", None))
    )


class ManualFeed:
    """Persist unknown outcomes across restarts; no reset by another feed call."""

    def __init__(self, sensor):
        self.sensor = sensor
        self.store = Store(sensor.hass, 1, f"tuya_feeder.feed.{sensor.entry.entry_id}")
        self.pending = False
        self.uncertain = False
        self.state = "idle"
        self.error = None
        self.requested_at = None
        self.observed_at = None
        self.reported_at = None
        self.portions = None
        self._sent = False
        self._waiter = None
        self._unsubs = []
        self._persist_lock = asyncio.Lock()

    async def start(self):
        saved = await self.store.async_load()
        if saved is not None:
            # Never resume an interrupted command or trust a restored feeding flag.
            self.uncertain = saved.get("uncertain", True) is not False
            self.state = "unknown" if self.uncertain else "idle"
            self.error = (
                "Feeding result is unknown. Check the feeder before trying again."
                if self.uncertain
                else None
            )
        for event_type in ("localtuya_device_dp_triggered", "localtuya_status_update"):
            self._unsubs.append(
                self.sensor.hass.bus.async_listen(event_type, self.received)
            )

    async def persist(self):
        async with self._persist_lock:
            await self.store.async_save({"uncertain": self.uncertain})

    @property
    def attributes(self):
        return {
            "supports_manual_feed": ready(self.sensor)
            and not self.pending
            and not self.uncertain
            and not self.sensor._action_lock.locked(),
            "manual_feed_capability": manual_capability(self.sensor),
            "feeding_pending": self.pending,
            "feeding_state": self.state,
            "feeding_error": self.error,
            "feeding_requested_at": self.requested_at,
            "feeding_observed_at": self.observed_at,
            "feeding_reported_at": self.reported_at,
            "feeding_portions": self.portions,
        }

    @callback
    def received(self, event):
        data = event.data
        if data.get("device_id") != self.sensor.entry.data["device_id"]:
            return
        # Availability/safety changes must update the card even without an
        # outstanding command; otherwise an initially offline feeder stays
        # disabled forever. These reports never start a feed or confirm one.
        if not self._sent:
            self.sensor._publish()
            return
        if not genuine_reports_allowed(
            self.sensor.hass, self.sensor.entry, self.sensor.audited
        ) or event.time_fired <= dt_util.parse_datetime(self.requested_at):
            return
        if event.event_type == "localtuya_device_dp_triggered":
            dps = {str(data.get("dp")): data.get("value")}
        else:
            # Audited status_update new_status is not evidence of which DPs were
            # received. Only the protocol's currently dispatched native frame is.
            device = runtime_device(self.sensor.hass, self.sensor.entry)
            dps = getattr(getattr(device, "_interface", None), "dispatched_dps", {})
            if not isinstance(dps, dict) or len(dps) < 2:
                return  # Single-DP frames are handled exclusively above.
        if dps.get("4") == "feeding":
            self.observed_at = event.time_fired.isoformat()
            self.state = "feeding"
        value = dps.get("14")
        if self.observed_at and type(value) is int and value == self.portions:
            self.reported_at = event.time_fired.isoformat()
            self.state = "reported"
            self.error = None
            self.uncertain = False
            self._sent = False
            if self._waiter and not self._waiter.done():
                self._waiter.set_result(True)
            self.sensor.hass.async_create_task(self.persist())
        self.sensor._publish()

    async def execute(self, portions):
        if type(portions) is not int or not 1 <= portions <= 60:
            raise ServiceValidationError("portions must be an integer from 1 to 60")
        if self.sensor._action_lock.locked() or self.pending:
            raise ServiceValidationError("A feeder action is already pending")
        if self.uncertain:
            raise ServiceValidationError(
                "Feeding result is unknown. Check the feeder before trying again."
            )
        if not ready(self.sensor):
            raise ServiceValidationError(
                "Feeder is unavailable, busy, has a fault, or is not in supported power mode"
            )
        async with self.sensor._action_lock:
            self.pending = True
            self.uncertain = True
            self.state = "command_pending"
            self.error = None
            self.portions = portions
            self.requested_at = None
            self.observed_at = self.reported_at = None
            self._waiter = self.sensor.hass.loop.create_future()
            self.sensor._publish()
            try:
                # Crash-safe reservation before any write. Do not auto-unlock
                # ambiguous failures, even when no incoming completion appears.
                await self.persist()
                if not ready(self.sensor):
                    self.uncertain = False
                    self.state = "idle"
                    self.error = "Feeder changed before send. No command sent."
                    raise ServiceValidationError(self.error)
                interface = runtime_device(
                    self.sensor.hass, self.sensor.entry
                )._interface
                payload = interface._generate_payload(7, {"3": portions})
                self.requested_at = dt_util.utcnow().isoformat()
                self._sent = True
                async with asyncio.timeout(self.sensor.feed_confirm_timeout):
                    await interface.exchange_quick(payload, recv_retries=0)
                    # Retain the legacy 2s guard as a minimum cooldown, NOT a
                    # delay before a second quick_feed actuation.
                    await asyncio.sleep(self.sensor.feed_guard_delay)
                    await self._waiter
            except ServiceValidationError:
                raise
            except asyncio.CancelledError:
                self.state = "unknown"
                self.error = "Feeding interrupted. It may have fed; do not retry."
                self.uncertain = True
                raise
            except Exception:
                self.state = "unknown"
                self.error = "Feeding result is unknown. It may have fed; do not retry."
                self.uncertain = True
            finally:
                self.pending = False
                if self._waiter and not self._waiter.done():
                    self._waiter.cancel()
                self._waiter = None
                await self.persist()
                self.sensor._publish()
            return {
                "command_attempted": self.requested_at is not None,
                "reported": self.state == "reported" and not self.uncertain,
                "state": self.state,
                "error": self.error,
                "requested_at": self.requested_at,
                "reported_at": self.reported_at,
            }

    async def stop(self):
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.persist()
