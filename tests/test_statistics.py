"""Offline tests for the statistics the coordinator writes.

This is the code whose mistakes are expensive: a wrong running sum or a
double-counted boundary point lands in Home Assistant's long-term
statistics, where it shows up as a spike on the Energy dashboard and has to
be deleted by hand. _build_statistics touches no instance state, so it can
be exercised directly without standing up Home Assistant.
"""
from __future__ import annotations

import pytest
from homeassistant.components.recorder.statistics import valid_statistic_id
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.origin_energy.api import UsageWindow
from custom_components.origin_energy.coordinator import (
    _ImportCursor,
    build_statistics,
    statistic_slug,
)


def point(hour, energy, cost=None, offset="+10:00", day=22):
    """One data point; energy=None models a MISSING bucket."""
    return {
        "type": "ACTUAL" if energy is not None else "MISSING",
        "startDate": f"2026-07-{day:02d}T{hour:02d}:00:00.000{offset}",
        "totalConsumedEnergy": energy,
        "totalCost": cost,
    }


def window(points, consumption_unit="WATT_HOUR", cost_unit="CENT"):
    return UsageWindow(
        points=points,
        last_reading_date=None,
        consumption_unit=consumption_unit,
        cost_unit=cost_unit,
        has_feed_in=False,
        has_missing_points=any(p["totalConsumedEnergy"] is None for p in points),
    )


def build(result, cursor, time_unit="HOURLY"):
    return build_statistics(result, cursor, time_unit)


# ---------------------------------------------------------------- running sums

@pytest.fixture
def three_points():
    cursor = _ImportCursor()
    consumption, _ = build(window([point(1, 1000), point(2, 2000), point(3, 500)]), cursor)
    return consumption, cursor


def test_converts_wh_to_kwh(three_points):
    consumption, _ = three_points
    assert [c["state"] for c in consumption] == [1.0, 2.0, 0.5]


def test_accumulates_the_sum(three_points):
    consumption, _ = three_points
    assert [c["sum"] for c in consumption] == [1.0, 3.0, 3.5]


def test_cursor_carries_the_total(three_points):
    _, cursor = three_points
    assert cursor.consumption_sum == 3.5


def test_cursor_tracks_the_last_point(three_points):
    _, cursor = three_points
    assert cursor.last_start.hour == 3


def test_second_window_resumes(three_points):
    _, cursor = three_points
    consumption, _ = build(window([point(4, 1500)]), cursor)
    assert [c["sum"] for c in consumption] == [5.0]


# ------------------------------------------------------------- MISSING buckets

@pytest.fixture
def with_gap():
    consumption, _ = build(
        window([point(1, 1000), point(2, None), point(3, 500)]), _ImportCursor()
    )
    return consumption


def test_skips_null_consumption(with_gap):
    assert [c["state"] for c in with_gap] == [1.0, 0.5]


def test_sum_ignores_the_gap(with_gap):
    assert [c["sum"] for c in with_gap] == [1.0, 1.5]


# ----------------------------------------------------- boundary de-duplication

# Origin's ranges are inclusive at both ends, so consecutive windows repeat
# the shared point; counting it twice would inflate the sum.

@pytest.fixture
def deduplicated():
    cursor = _ImportCursor(
        last_start=dt_util.parse_datetime("2026-07-22T02:00:00.000+10:00")
    )
    consumption, _ = build(window([point(1, 1000), point(2, 2000), point(3, 500)]), cursor)
    return consumption, cursor


def test_drops_already_imported_points(deduplicated):
    consumption, _ = deduplicated
    assert [c["state"] for c in consumption] == [0.5]


def test_sum_counts_the_new_point_only(deduplicated):
    _, cursor = deduplicated
    assert cursor.consumption_sum == 0.5


# ------------------------------------------------------------- cost conversion

def test_converts_cents_to_dollars():
    _, cost = build(window([point(1, 493, 22.143)]), _ImportCursor())
    assert round(cost[0]["state"], 5) == 0.22143


def test_treats_a_null_cost_as_zero():
    _, cost = build(window([point(1, 493)]), _ImportCursor())
    assert cost[0]["state"] == 0.0


# --------------------------------------------------- declared units are honoured

@pytest.fixture
def native_units():
    return build(
        window([point(1, 2.5, 1.25)], consumption_unit="KILOWATT_HOUR", cost_unit="DOLLAR"),
        _ImportCursor(),
    )


def test_kwh_passes_through_unscaled(native_units):
    consumption, _ = native_units
    assert consumption[0]["state"] == 2.5


def test_dollars_pass_through_unscaled(native_units):
    _, cost = native_units
    assert cost[0]["state"] == 1.25


def test_scales_mwh():
    consumption, _ = build(window([point(1, 1)], consumption_unit="MEGAWATT_HOUR"), _ImportCursor())
    assert consumption[0]["state"] == 1000.0


# --------------------------------------------------- unknown units refuse to guess

def test_unknown_energy_unit_raises():
    with pytest.raises(UpdateFailed, match="FURLONGS"):
        build(window([point(1, 1000)], consumption_unit="FURLONGS"), _ImportCursor())


def test_unknown_cost_unit_raises():
    with pytest.raises(UpdateFailed, match="DOUBLOON"):
        build(window([point(1, 1000)], cost_unit="DOUBLOON"), _ImportCursor())


# ------------------------------------------------------ end of daylight saving

# Sydney repeats 02:00 on the first Sunday in April: once at +11:00, then an
# hour later at +10:00. Sorted as text the later point comes first, which
# would make the running sums go backwards -- so the parsed instants have to
# drive the order.

FOLDED = [
    point(2, 1000, offset="+10:00", day=5),  # chronologically second
    point(2, 2000, offset="+11:00", day=5),  # chronologically first
    point(3, 3000, offset="+10:00", day=5),
]


def test_the_apis_string_order_is_the_wrong_order():
    assert [p["startDate"] for p in FOLDED] == sorted(p["startDate"] for p in FOLDED)


@pytest.fixture
def folded():
    consumption, _ = build(window(FOLDED), _ImportCursor())
    return consumption


def test_re_sorts_the_repeated_hour(folded):
    assert [c["state"] for c in folded] == [2.0, 1.0, 3.0]


def test_sums_stay_monotonic_across_the_fold(folded):
    assert [c["sum"] for c in folded] == [2.0, 3.0, 6.0]


def test_no_point_is_dropped_in_the_fold(folded):
    assert len(folded) == 3


# ------------------------------------------ statistic ids Home Assistant accepts

@pytest.mark.parametrize(
    ("raw", "slug"),
    [
        ("40318827", "40318827"),  # plain digits pass through
        ("A-1B2C-3D4", "a_1b2c_3d4"),  # hyphens become underscores
        ("--A??B--", "a_b"),  # runs collapse, edges trimmed
        ("!!!", "unknown"),  # an unusable id still yields something
    ],
)
def test_statistic_slug(raw, slug):
    assert statistic_slug(raw) == slug


@pytest.mark.parametrize("raw", ["40318827", "A-1B2C3D4E-4031", "acct/99", "  x  "])
def test_slugged_ids_are_valid_statistic_ids(raw):
    assert valid_statistic_id(f"origin_energy:{statistic_slug(raw)}_energy_consumption")


# ------------------------------------------------- stored on UTC clock hours

@pytest.fixture
def late_evening():
    consumption, _ = build(window([point(23, 493)]), _ImportCursor())
    return consumption


def test_stored_in_utc(late_evening):
    assert late_evening[0]["start"].utcoffset().total_seconds() == 0.0


def test_stays_on_the_clock_hour(late_evening):
    assert late_evening[0]["start"].minute == 0
