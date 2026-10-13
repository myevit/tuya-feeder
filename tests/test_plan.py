"""Deliberately synthetic plans, never household captures."""

import base64

import pytest

from custom_components.tuya_feeder.plan import DAYS, decode, encode


def meal(**kwargs):
    return {
        "days": list(DAYS),
        "time": "03:21",
        "portions": 2,
        "enabled": False,
        **kwargs,
    }


@pytest.mark.parametrize("count", range(1, 11))
def test_roundtrip_padding(count):
    meals = [meal(time=f"{i:02d}:21") for i in range(count)]
    raw = encode(meals)
    assert decode(raw) == meals
    assert len(base64.b64decode(raw)) == count * 5


@pytest.mark.parametrize(
    "change",
    [
        {"portions": True},
        {"portions": 0},
        {"portions": 61},
        {"portions": "2"},
        {"enabled": 1},
        {"enabled": "false"},
        {"time": "24:00"},
        {"time": "00:60"},
        {"time": "1:00"},
        {"days": ["monday"]},
        {"days": list(DAYS) + ["monday"]},
        {"extra": 1},
        {"days": "every_day"},
        {"time": None},
    ],
)
def test_invalid_meals(change):
    with pytest.raises(ValueError):
        encode([meal(**change)])


@pytest.mark.parametrize("value", [None, {}, "bad", [None], [meal()] * 11])
def test_invalid_container(value):
    with pytest.raises(ValueError):
        encode(value)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "!",
        "AA==",
        base64.b64encode(bytes([1, 3, 21, 2, 0])).decode(),
        base64.b64encode(bytes([127, 3, 21, 2, 2])).decode(),
    ],
)
def test_fail_closed_decode(raw):
    with pytest.raises(ValueError):
        decode(raw)


def test_empty_plan_and_exact_preservation():
    assert decode(encode([])) == []
    raw = base64.b64encode(bytes([127, 3, 21, 2, 0, 127, 4, 22, 3, 1])).decode()
    assert encode(decode(raw)) == raw
