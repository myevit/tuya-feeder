"""Last genuinely received schedule plus an explicitly confirmed local writer."""

import asyncio
from copy import deepcopy

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .feed import ManualFeed
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
        self.snapshot_source = "unknown"
        self.imported_from = None
        self.error = None
        self.pending = False
        self.uncertain = False
        self.confirm_timeout = 15.0
        self._sent = False
        self._target = None
        self._waiter = None
        self._unsub = None
        self._save_lock = asyncio.Lock()
        self._action_lock = asyncio.Lock()
        self.feed_confirm_timeout = 120.0
        self.feed_guard_delay = 2.0
        self.manual = ManualFeed(self)

    async def start(self):
        """Restore only our previously received private snapshot, then listen."""
        saved = await self.store.async_load()
        if saved is not None:
            try:
                # Malformed reports retain metadata but never their raw payload.
                meals = decode(saved["raw"]) if saved["raw"] is not None else None
                revision = saved["revision"]
                timestamp = saved["last_received_at"]
                parsed = (
                    dt_util.parse_datetime(timestamp)
                    if type(timestamp) is str
                    else None
                )
                if (
                    type(revision) is not int
                    or revision < 1
                    or type(timestamp) is not str
                    or parsed is None
                    or parsed.tzinfo is None
                ):
                    raise ValueError("Invalid stored metadata")
                self.raw, self.meals = saved["raw"], meals
                self.revision, self.last_received_at = revision, timestamp
                self.snapshot_source = "restored"
                self.error = saved.get("error")
                self.uncertain = bool(saved.get("uncertain", False))
                self.imported_from = saved.get("imported_from")
            except (KeyError, TypeError, ValueError):
                self.error = "invalid_stored_snapshot"
        await self.manual.start()
        self._unsub = self.hass.bus.async_listen(
            "localtuya_device_dp_triggered", self._received
        )

    async def save(self):
        """Serialize disk saves so the last report wins even during a write."""
        async with self._save_lock:
            if self.revision < 1:
                await self.store.async_remove()
            else:
                await self.store.async_save(
                    {
                        "raw": self.raw,
                        "revision": self.revision,
                        "last_received_at": self.last_received_at,
                        "error": self.error,
                        "uncertain": self.uncertain,
                        "imported_from": self.imported_from,
                    }
                )

    @callback
    def _publish(self):
        if self.entity_id is not None:
            self.async_write_ha_state()

    async def import_snapshot(self, source_entity_id):
        """Import explicitly selected passive historical data, never a receipt.

        HA attributes are user-trusted metadata, not cryptographic device proof.
        Only a cold unknown baseline can be initialized; no unsafe state recovery.
        """
        if (
            not source_entity_id.startswith("sensor.")
            or source_entity_id == self.entity_id
            or self.entry.options.get("historical_snapshot_entity_id")
            != source_entity_id
        ):
            raise ServiceValidationError(
                "Select this historical source in integration options first"
            )
        if self.pending or self.revision != 0 or self.uncertain or self.error:
            raise ServiceValidationError(
                "Import is only allowed for an empty initial snapshot"
            )
        state = self.hass.states.get(source_entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            raise ServiceValidationError("Historical source is unavailable")
        attrs = state.attributes
        timestamp = attrs.get("received_at")
        parsed = (
            dt_util.parse_datetime(timestamp) if isinstance(timestamp, str) else None
        )
        if (
            attrs.get("source") != "LocalTuya LAN DP1 report"
            or attrs.get("read_only") not in (True, "True")
            or parsed is None
            or parsed.tzinfo is None
            or parsed > dt_util.utcnow()
        ):
            raise ServiceValidationError(
                "Source must be a passive LAN DP1 snapshot with its original timezone-aware receipt timestamp"
            )
        raw = attrs.get("raw_plan")
        try:
            meals = decode(raw)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        self.raw, self.meals = raw, meals
        self.revision = 1
        self.last_received_at = timestamp
        self.snapshot_source = "restored"
        self.imported_from = source_entity_id
        await self.save()
        self._publish()

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
        self.snapshot_source = "received"
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
            **self.manual.attributes,
            "meals": deepcopy(self.meals) if self.meals is not None else [],
            "revision": self.revision,
            "last_received_at": self.last_received_at,
            "observation_kind": OBSERVATION_KIND,
            "snapshot_source": self.snapshot_source,
            "imported_from": self.imported_from,
            "requires_restored_snapshot_acknowledgement": self.snapshot_source
            == "restored",
            "supports_restored_schedule_write": self.meals is not None
            and self.snapshot_source == "restored"
            and not self.uncertain
            and not self.error
            and not self._action_lock.locked()
            and not self.manual.uncertain
            and can_send(self.hass, self.entry, self.audited),
            "schedule_valid": self.meals is not None,
            "pending": self.pending,
            "error": self.error,
            "supports_schedule_write": self.meals is not None
            and self.snapshot_source == "received"
            and not self.uncertain
            and not self._action_lock.locked()
            and not self.manual.uncertain
            and can_send(self.hass, self.entry, self.audited),
            "supported_days_mode": "every_day",
            "max_meals": MAX_MEALS,
            "max_portions": MAX_PORTIONS,
            "warning": "Ordinary LAN GET omits DP1. External app changes may not be reported; revision only guards observed HA edits.",
        }

    async def set_schedule(
        self, expected_revision, meals, acknowledge_restored_snapshot=False
    ):
        """Reject busy operations, rather than queueing stale user requests."""
        if self._action_lock.locked() or self.manual.uncertain:
            raise ServiceValidationError(
                "A feeder action is already pending or its result is unknown"
            )
        try:
            async with self._action_lock:
                return await self._set_schedule(
                    expected_revision, meals, acknowledge_restored_snapshot
                )
        finally:
            self._publish()

    async def meal_action(
        self,
        action,
        expected_revision,
        meal_index=None,
        meal=None,
        acknowledge_restored_snapshot=False,
    ):
        try:
            return await self._meal_action(
                action,
                expected_revision,
                meal_index,
                meal,
                acknowledge_restored_snapshot,
            )
        finally:
            self._publish()

    async def _meal_action(
        self,
        action,
        expected_revision,
        meal_index=None,
        meal=None,
        acknowledge_restored_snapshot=False,
    ):
        """Merge under the device action lock; no await before snapshot validation."""
        if self._action_lock.locked() or self.manual.uncertain:
            raise ServiceValidationError(
                "A feeder action is already pending or its result is unknown"
            )
        async with self._action_lock:
            if type(expected_revision) is not int or expected_revision != self.revision:
                raise ServiceValidationError(
                    "Stale expected_revision; reload the last reported plan"
                )
            if self.meals is None:
                raise ServiceValidationError("No known valid meal plan")
            meals = deepcopy(self.meals)
            if action in ("update_meal", "delete_meal"):
                if type(meal_index) is not int or not 0 <= meal_index < len(meals):
                    raise ServiceValidationError(
                        "meal_index must identify a current meal"
                    )
            if action == "update_meal":
                meals[meal_index] = meal
            elif action == "add_meal":
                meals.append(meal)
            elif action == "delete_meal":
                if len(meals) == 1:
                    raise ServiceValidationError(
                        "The final meal cannot be removed yet; turn it off instead"
                    )
                del meals[meal_index]
            else:
                raise ServiceValidationError("Unsupported meal action")
            return await self._set_schedule(
                expected_revision, meals, acknowledge_restored_snapshot
            )

    async def _set_schedule(
        self, expected_revision, meals, acknowledge_restored_snapshot=False
    ):
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
            or not (
                self.snapshot_source == "received"
                or (
                    self.snapshot_source == "restored"
                    and acknowledge_restored_snapshot is True
                )
            )
            or self.uncertain
            or self.error
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

    async def feed(self, portions):
        try:
            return await self.manual.execute(portions)
        finally:
            self._publish()

    async def stop(self):
        await self.manual.stop()
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._waiter is not None and not self._waiter.done():
            self._waiter.cancel()
        await self.save()
