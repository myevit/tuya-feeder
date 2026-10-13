# Tuya Feeder

[![Validate](https://github.com/myevit/tuya-feeder/actions/workflows/validate.yml/badge.svg)](https://github.com/myevit/tuya-feeder/actions/workflows/validate.yml)

A local Home Assistant integration for **FT-11W feeder schedules**. It displays the last genuinely received DP1 meal plan and provides a validated, complete-plan writer through an **existing LocalTuya connection**.

**This is not an immediate feeding integration.** It exposes no feeding commands, creates no recurring HA feeding automations, opens no additional socket, and makes no cloud calls. The feeder continues executing its stored schedules without Home Assistant or Internet access.

## Compatibility and important limits

- Initial release audits **xZetsubou/hass-localtuya 2026.07.0**, a directly connected Wi-Fi FT-11W, and a **non-write-only** LocalTuya device runtime. Other forks/versions and gateway subdevices fail closed: neither their events nor their optimistic caches establish a writable snapshot. Upgrade support requires a new source audit, not removing the version guard.
- DP1 records are five bytes: every-day mask `127`, hour, minute, portions, enabled (`0` or `1`). Only **every day** is verified. Individual weekday bit order is **not verified** and is intentionally unsupported.
- Maximum ten meals (disabled records count), portions 1–60. Times use the feeder's existing local clock; this integration does not set its timezone or daylight-saving behavior. Portions are not grams.
- **Normal LAN GET omits DP1.** This sensor is a *last locally reported snapshot*, not an active schedule query. A recent timestamp does not guarantee an external app has not changed the plan. There is no fake refresh/query button.
- Revisions prevent concurrent/stale edits **within the observed HA history**; they cannot guarantee protection against unreported external-app edits. Before using the writer, understand that a complete replacement may overwrite unobserved changes.
- No unknown record is silently dropped. Unknown, malformed, partial, or unsupported plans disable writing. An initial unknown sensor remains unknown until a genuine DP1 event is received; this release has no cloud/bootstrap fallback and does not write a schedule to discover it.
- A Home Assistant bus event is trusted local-process evidence, not cryptographic attestation: privileged users/integrations can fabricate events. Restrict HA access accordingly.

## Install with HACS

1. Install/configure the compatible [LocalTuya fork](https://github.com/xZetsubou/hass-localtuya) normally. Verify the correct FT-11W is selected and its runtime is not write-only. This integration never asks for or copies a local key.
2. In HACS → menu → **Custom repositories**, add `https://github.com/myevit/tuya-feeder`, category **Integration**, then download **Tuya Feeder**.
3. Restart Home Assistant as required for a newly installed custom integration.
4. Settings → Devices & services → Add integration → **Tuya Feeder**. Select the existing feeder. Selection does not query, write, or dispense.
5. Use the new meal-plan sensor's attributes. An optional separate dashboard card is maintained at [myevit/tuya-feeder-card](https://github.com/myevit/tuya-feeder-card).

This is usable as a HACS **custom repository**. Default-catalog submission is a separate process and is not claimed. Original integration icons are included in `custom_components/tuya_feeder/brand/` (HA 2026.3+ local brands); older HA versions use the fallback icon. The HACS legacy central-brand check is skipped, not the integration validation.

Manual installation: copy **only** `custom_components/tuya_feeder` into your HA `custom_components` directory. Do not copy tests or development environments. HA 2026.2+ is required; tests currently exercise HA 2026.2.3.

## Sensor/API contract

Domain: `tuya_feeder`. The sensor state is meal count or `unknown`, never a claim that the feeder is currently executing a schedule. It is nonpolling and restores its own private `.storage` snapshot. Restored snapshots remain read only until a new genuine valid local report arrives, even when their saved timestamp looks recent. Use integration and card **v0.1.1 or newer together**.

| Attribute | Meaning |
| --- | --- |
| `meals` | Complete list of `{days, time, portions, enabled}`; `[]` if unknown, distinguish with unknown state/support flag |
| `revision` | Integer observation revision; increases on every accepted DP1 report, even identical or malformed reports |
| `last_received_at` | ISO timestamp of the last accepted device report, **not** a GET or write time |
| `observation_kind` | Explicit last-locally-reported snapshot warning; never relabeled as an active query or `confirmed` |
| `schedule_valid` | Entire snapshot passed the strict daily DP1 decoder, including known-empty reports |
| `snapshot_source` | `received` for a genuine report in this runtime; `restored` for saved historical data; `unknown` before either |
| `pending` | A single write is reserved/in progress |
| `error` | `null` or an explicit validation/report/confirmation error |
| `supports_schedule_write` | Known supported snapshot received in this runtime, compatible connected transport, no unresolved write uncertainty |
| `supported_days_mode` | `every_day` |
| `max_meals` / `max_portions` | `10` / `60` |
| `warning` | External-app stale-cache limitation |

### `tuya_feeder.set_schedule`

Required payload: **one** `entity_id`, integer `expected_revision`, and `meals` list. Each meal must have *exactly* these fields:

```yaml
# Deliberately synthetic example: DO NOT run as a test on a real feeder.
entity_id: sensor.example_meal_plan
expected_revision: 7  # Replace with the current sensor revision.
meals:
  - days: [monday, tuesday, wednesday, thursday, friday, saturday, sunday]
    time: "03:21"
    portions: 2
    enabled: false
```

The list is a **complete replacement**, not a patch. Include every existing meal you intend to retain, preserving disabled entries and ordering. No records are deduplicated or reordered. `meals: []` intentionally removes all scheduled meals and is a real schedule change. Duplicate times remain duplicate records: the device's behavior for duplicate times is not claimed.

The writer validates before touching transport, checks revision and reserves one pending operation, persists uncertainty, then sends DP1 **once** via `localtuya.set_dp`. It waits up to 15 seconds for an exact-device `localtuya_device_dp_triggered` DP1 value matching the entire serialized payload. Normal status updates and optimistic set caches are ignored. Reports arriving before sending cannot confirm a write. A matching report means local DP1 acknowledgement, **not** a verified physical dispense or future execution.

When optional HA action responses are requested, returns:

```json
{"confirmed": true, "revision": 8, "error": null}
```

Timeout or transport failure returns `confirmed: false` with `unconfirmed_timeout` or `unconfirmed_transport_error`; never automatic retry. Without requested responses, an unconfirmed outcome raises an action error as well as updating the sensor. The device **may have applied the change even when confirmation fails**. Do not blindly resubmit. Writes remain blocked until a later genuine valid report resolves uncertainty, including across reload/restart. A fully unchanged plan sends nothing and returns `confirmed: false`, `error: "unchanged_no_write"`; this does not pretend that a new report arrived. A revision change before sending yields `changed_before_send_no_write`; a later conflicting report can yield `unconfirmed_superseded`.

Invalid data, stale revision, unavailable/incompatible transport, wrong entity, pending operation, unknown snapshot, or uncertainty raises a service validation error and performs **no write**. Booleans are not accepted as integers. No coercion from numeric strings, loose times, or truthy enabled values occurs.

## Preservation / removal

Installing this integration does not alter existing LocalTuya entities, custom passive sensors, schedules, disabled records, dashboards, or automations. It never writes automatically on setup, restore, polling, reload, or unload. Explicit removal deletes only its own private snapshot. Config-entry removal/recreation starts a new revision history; clients must reload rather than reuse old revisions.

Before any manual migration, back up your existing HA configuration and private `.storage` data using your normal secure backup process. That data can contain household schedules and secrets. Never attach it to public issues. Report problems with **synthetic** examples and version information only. No production deployment or physical feeding test is implied by automated tests.

## Development / tests

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest -q --cov=custom_components.tuya_feeder
.venv/bin/ruff check .
.venv/bin/ruff format --check .
python3 scripts/check_public.py
```

The tests use actual Home Assistant config entries, entity platforms, action registry, event bus and private storage through `pytest-homeassistant-custom-component`. LocalTuya service calls are **intercepted**; outbound sockets are disabled. Fixtures are deliberately synthetic, not redacted household captures. CI includes tests, lint, hassfest, HACS validation, and a public-file safety check.

## Sources / provenance

- [HA manifests](https://developers.home-assistant.io/docs/creating_integration_manifest/)
- [HA config flows](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/)
- [HA service actions / optional responses](https://developers.home-assistant.io/docs/dev_101_services/)
- [HACS integration requirements](https://www.hacs.xyz/docs/publish/integration/), [validation action](https://www.hacs.xyz/docs/publish/action/)
- [HA local integration brands](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api)
- [Audited LocalTuya coordinator](https://github.com/xZetsubou/hass-localtuya/blob/2026.7.0/custom_components/localtuya/coordinator.py), [service handler](https://github.com/xZetsubou/hass-localtuya/blob/2026.7.0/custom_components/localtuya/__init__.py)

Installed LocalTuya source was inspected read-only: `status_updated` emits single received-packet DP events; `set_status` synthesizes `status_updated` only in write-only mode. The audited Wi-Fi `is_write_only` property may return `None`/an empty node ID (its short-circuit false value), so the guard accepts those audited values but rejects a missing property. DP1 format and opcode-8 local acknowledgement were verified during separate private research; **no household schedules, IDs, addresses, keys, captures, or configs are published here**. Tests establish code behavior, not additional hardware compatibility.

Unofficial project; not affiliated with Tuya. Code and original brand artwork: MIT license.
