"""Last genuinely received schedule plus an explicitly confirmed local writer."""

import asyncio
from copy import deepcopy

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .plan import MAX_MEALS, MAX_PORTIONS, decode, encode
from .transport import can_send, genuine_reports_allowed

OBSERVATION_KIND = "last locally reported snapshot; not an active schedule query"


async def async_setup_entry(hass, entry, async_add_entities):
    """Add the already-subscribed entity."""
    async_add_entities([hass.data["tuya_feeder"][entry.entry_id]])


class MealPlanSensor(SensorEntity):
    """Never use LocalTuya's optimistic cache as received evidence."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Meal plan"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, hass, entry, audited):
        self.hass = hass
        self.entry = entry
        self.audited = audited
        self._attr_unique_id = f"{entry.unique_id}_meal_plan"
        self.store = Store(hass, 1, f"tuya_feeder.{entry.entry_id}")
        self.raw = None
        self.meals = None
        self.revision = 0
        self.last_received_at = None
        self.error = None
        self.pending = False
        self.uncertain = False
        self.confirm_timeout = 15.0
        self._sent = False
        self._target = None
        self._waiter = None
        self._unsub = None
        self._save_lock = asyncio.Lock()

    async def start(self):
        """Restore only our previously received private snapshot, then listen."""
        saved = await self.store.async_load()
        if saved is not None:
            try:
                meals = decode(saved["raw"])
                revision = saved["revision"]
                timestamp = saved["last_received_at"]
                if (
                    type(revision) is not int
                    or revision < 1
                    or type(timestamp) is not str
                    or dt_util.parse_datetime(timestamp) is None
                ):
                    raise ValueError("Invalid stored metadata")
                self.raw, self.meals = saved["raw"], meals
                self.revision, self.last_received_at = revision, timestamp
                self.error = saved.get("error")
                self.uncertain = bool(saved.get("uncertain", False))
            except (KeyError, TypeError, ValueError):
                self.error = "invalid_stored_snapshot"
        self._unsub = self.hass.bus.async_listen(
            "localtuya_device_dp_triggered", self._received
        )

    async def save(self):
        """Serialize disk saves so the last report wins even during a write."""
        async with self._save_lock:
            if self.raw is None:
                await self.store.async_remove()
            else:
                await self.store.async_save(
                    {
                        "raw": self.raw,
                        "revision": self.revision,
                        "last_received_at": self.last_received_at,
                        "error": self.error,
                        "uncertain": self.uncertain,
                    }
                )

    @callback
    def _publish(self):
        if self.entity_id is not None:
            self.async_write_ha_state()

    @callback
    def _received(self, event):
        """Only exact-device single-DP received events from the audited runtime."""
        data = event.data
        if (
            data.get("device_id") != self.entry.data["device_id"]
            or data.get("dp") not in ("1", 1)
            or not genuine_reports_allowed(self.hass, self.entry, self.audited)
        ):
            return
        raw = data.get("value")
        self.revision += 1
        self.last_received_at = event.time_fired.isoformat()
        try:
            meals = decode(raw)
        except ValueError:
            self.meals = None
            self.raw = None
            self.error = "unsupported_or_malformed_report"
            self.uncertain = True
            # Remove the older valid cache; never resurrect it after restart.
            self.hass.async_create_task(self.save())
        else:
            self.raw, self.meals = raw, meals
            self.error = None
            self.uncertain = False
            self.hass.async_create_task(self.save())
            if (
                self.pending
                and self._sent
                and raw == self._target
                and self._waiter is not None
                and not self._waiter.done()
            ):
                self._waiter.set_result(True)
        self._publish()

    @property
    def native_value(self):
        return len(self.meals) if self.meals is not None else None

    @property
    def extra_state_attributes(self):
        return {
            "meals": deepcopy(self.meals) if self.meals is not None else [],
            "revision": self.revision,
            "last_received_at": self.last_received_at,
            "observation_kind": OBSERVATION_KIND,
            "pending": self.pending,
            "error": self.error,
            "supports_schedule_write": self.meals is not None
            and not self.uncertain
            and can_send(self.hass, self.entry, self.audited),
            "supported_days_mode": "every_day",
            "max_meals": MAX_MEALS,
            "max_portions": MAX_PORTIONS,
            "warning": "Ordinary LAN GET omits DP1. External app changes may not be reported; revision only guards observed HA edits.",
        }

    async def set_schedule(self, expected_revision, meals):
        """One full-plan DP1 write, never retry or claim a sent value as received."""
        try:
            target = encode(meals)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        if self.pending:
            raise ServiceValidationError("A schedule write is already pending")
        if type(expected_revision) is not int or expected_revision != self.revision:
            raise ServiceValidationError(
                "Stale expected_revision; reload the last reported plan"
            )
        if (
            self.meals is None
            or self.uncertain
            or not can_send(self.hass, self.entry, self.audited)
        ):
            raise ServiceValidationError(
                "No safe locally reported snapshot or compatible connected transport"
            )
        if target == self.raw:
            return {
                "confirmed": False,
                "revision": self.revision,
                "error": "unchanged_no_write",
            }
        # No await before reservation: concurrent HA calls cannot pass together.
        self.pending = True
        self._target = target
        self._waiter = self.hass.loop.create_future()
        self.error = None
        self._publish()
        try:
            # Persist uncertainty before transport: a process crash must not turn
            # an unconfirmed write into a safe restored baseline.
            self.uncertain = True
            await self.save()
            if self.revision != expected_revision or not can_send(
                self.hass, self.entry, self.audited
            ):
                self.error = "changed_before_send_no_write"
                return {
                    "confirmed": False,
                    "revision": self.revision,
                    "error": self.error,
                }
            self._sent = True
            async with asyncio.timeout(self.confirm_timeout):
                await self.hass.services.async_call(
                    "localtuya",
                    "set_dp",
                    {
                        "device_id": self.entry.data["device_id"],
                        "dp": 1,
                        "value": target,
                    },
                    blocking=True,
                )
                await self._waiter
            confirmed = self.raw == target and not self.uncertain
            self.error = None if confirmed else "unconfirmed_superseded"
            if not confirmed:
                self.uncertain = True
        except TimeoutError:
            self.error = "unconfirmed_timeout"
            self.uncertain = True
            confirmed = False
        except Exception:  # LocalTuya may propagate non-HA transport exceptions.
            self.error = "unconfirmed_transport_error"
            self.uncertain = True
            confirmed = False
        except asyncio.CancelledError:
            self.error = "unconfirmed_cancelled"
            self.uncertain = True
            raise
        finally:
            self.pending = False
            self._sent = False
            self._target = None
            if self._waiter is not None and not self._waiter.done():
                self._waiter.cancel()
            self._waiter = None
            await self.save()
            self._publish()
        return {"confirmed": confirmed, "revision": self.revision, "error": self.error}

    async def stop(self):
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._waiter is not None and not self._waiter.done():
            self._waiter.cancel()
        await self.save()
