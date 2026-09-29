"""Statistics land on UTC hours in every Australian timezone.

Home Assistant checks a statistic's start is on the hour before converting
it to UTC, so a point at 10:00+09:30 would be accepted and stored at 00:30
UTC. These cover the half-hour zones (Adelaide, with and without daylight
saving, and Darwin), a whole-hour zone as a control, and daily data.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from custom_components.origin_energy.api import UsageWindow
from custom_components.origin_energy.coordinator import (
    HOUR,
    _ImportCursor,
    build_statistics,
    utc_hour,
)

ADELAIDE = ZoneInfo("Australia/Adelaide")  # +09:30, +10:30 in summer
DARWIN = ZoneInfo("Australia/Darwin")  # +09:30 all year
PERTH = ZoneInfo("Australia/Perth")  # +08:00, already on the UTC grid
SYDNEY = ZoneInfo("Australia/Sydney")


def hourly_points(tz, first_day: datetime, hours: int) -> list[dict]:
    """Consecutive local-clock-hour points, as Origin returns them.

    Stepped in absolute time from local midnight, so a daylight-saving day
    has 23 or 25 of them. Values vary so a misattributed half would show.
    """
    midnight = datetime(first_day.year, first_day.month, first_day.day, tzinfo=tz)
    points = []
    for i in range(hours):
        start = (midnight.astimezone(UTC) + i * HOUR).astimezone(tz)
        points.append({
            "type": "ACTUAL",
            "startDate": start.isoformat(),
            "totalConsumedEnergy": 100 * (i % 7 + 1),
            "totalCost": 3.0 * (i % 5 + 1),
        })
    return points


def daily_point(tz, day: datetime, energy: float = 12000, cost: float = 400) -> dict:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return {
        "type": "ACTUAL",
        "startDate": start.isoformat(),
        "totalConsumedEnergy": energy,
        "totalCost": cost,
    }


def window(points) -> UsageWindow:
    return UsageWindow(
        points=points,
        last_reading_date=None,
        consumption_unit="WATT_HOUR",
        cost_unit="CENT",
        has_feed_in=False,
        has_missing_points=False,
    )


def total_kwh(points) -> float:
    return sum(p["totalConsumedEnergy"] for p in points) / 1000


def pending_kwh(cursor) -> float:
    return sum(b.consumption for b in cursor.pending.values())


WINTER = datetime(2026, 7, 20)
# Adelaide moves from +09:30 to +10:30 at 02:00 local on 4 October 2026.
DST_START = datetime(2026, 10, 4)


@pytest.mark.parametrize(
    ("tz", "day", "hours"),
    [
        (ADELAIDE, WINTER, 48),  # +09:30
        (ADELAIDE, DST_START, 23 + 24),  # +09:30 then +10:30, a 23-hour day
        (DARWIN, WINTER, 48),  # +09:30, no daylight saving
        (DARWIN, DST_START, 48),  # unaffected by the SA change
    ],
)
class TestHalfHourZones:
    def test_every_start_is_a_utc_hour(self, tz, day, hours):
        consumption, cost = build_statistics(
            window(hourly_points(tz, day, hours)), _ImportCursor(), "HOURLY"
        )
        for row in consumption + cost:
            assert row["start"].utcoffset() == timedelta(0)
            assert (row["start"].minute, row["start"].second) == (0, 0)

    def test_hours_are_contiguous(self, tz, day, hours):
        consumption, _ = build_statistics(
            window(hourly_points(tz, day, hours)), _ImportCursor(), "HOURLY"
        )
        starts = [row["start"] for row in consumption]
        assert all(b - a == HOUR for a, b in pairwise(starts))

    def test_no_energy_is_created_or_lost(self, tz, day, hours):
        """Written plus held back equals what Origin reported."""
        points = hourly_points(tz, day, hours)
        cursor = _ImportCursor()
        consumption, _ = build_statistics(window(points), cursor, "HOURLY")
        written = sum(row["state"] for row in consumption)
        assert written + pending_kwh(cursor) == pytest.approx(total_kwh(points))
        assert consumption[-1]["sum"] == pytest.approx(written)

    def test_each_hour_is_half_of_each_neighbour(self, tz, day, hours):
        points = hourly_points(tz, day, hours)
        consumption, _ = build_statistics(window(points), _ImportCursor(), "HOURLY")
        # Point i covers [t, t+1h) with t on the half hour, so UTC hour
        # floor(t)+1h gets the second half of point i and the first half of
        # point i+1.
        for i, row in enumerate(consumption[1:]):
            expected = (points[i]["totalConsumedEnergy"]
                        + points[i + 1]["totalConsumedEnergy"]) / 2 / 1000
            assert row["state"] == pytest.approx(expected)

    def test_the_last_partial_hour_is_held_not_written(self, tz, day, hours):
        """Writing it half-full would be corrected later, but understate until then."""
        cursor = _ImportCursor()
        build_statistics(window(hourly_points(tz, day, hours)), cursor, "HOURLY")
        assert len(cursor.pending) == 1
        (bucket,) = cursor.pending.values()
        assert bucket.covered == HOUR / 2


class TestWholeHourZones:
    def test_perth_points_pass_through_unchanged(self):
        points = hourly_points(PERTH, WINTER, 24)
        cursor = _ImportCursor()
        consumption, _ = build_statistics(window(points), cursor, "HOURLY")
        assert [row["start"] for row in consumption] == [
            datetime.fromisoformat(p["startDate"]).astimezone(UTC) for p in points
        ]
        assert [row["state"] for row in consumption] == pytest.approx(
            [p["totalConsumedEnergy"] / 1000 for p in points]
        )
        assert not cursor.pending


class TestDaily:
    def test_adelaide_day_is_filed_whole_under_one_utc_hour(self):
        """Midnight +09:30 is 14:30 UTC; the day goes under 14:00, not split."""
        cursor = _ImportCursor()
        consumption, cost = build_statistics(
            window([daily_point(ADELAIDE, WINTER)]), cursor, "DAILY"
        )
        assert [row["start"] for row in consumption] == [
            datetime(2026, 7, 19, 14, 0, tzinfo=UTC)
        ]
        assert consumption[0]["state"] == pytest.approx(12.0)
        assert cost[0]["state"] == pytest.approx(4.0)
        assert not cursor.pending

    def test_adelaide_summer_day(self):
        """Midnight +10:30 is 13:30 UTC."""
        day = datetime(2026, 12, 1)
        consumption, _ = build_statistics(
            window([daily_point(ADELAIDE, day)]), _ImportCursor(), "DAILY"
        )
        assert consumption[0]["start"] == datetime(2026, 11, 30, 13, 0, tzinfo=UTC)

    def test_sydney_day_is_unchanged(self):
        consumption, _ = build_statistics(
            window([daily_point(SYDNEY, WINTER)]), _ImportCursor(), "DAILY"
        )
        assert consumption[0]["start"] == datetime(2026, 7, 19, 14, 0, tzinfo=UTC)

    def test_daily_totals_are_unchanged(self):
        days = [WINTER + timedelta(days=i) for i in range(5)]
        points = [daily_point(ADELAIDE, d, energy=1000 * (i + 1)) for i, d in enumerate(days)]
        consumption, _ = build_statistics(window(points), _ImportCursor(), "DAILY")
        assert [row["state"] for row in consumption] == pytest.approx([1, 2, 3, 4, 5])


class TestAcrossImports:
    def test_windows_split_anywhere_give_the_same_rows(self):
        points = hourly_points(ADELAIDE, WINTER, 72)
        whole, _ = build_statistics(window(points), _ImportCursor(), "HOURLY")

        cursor = _ImportCursor()
        first, _ = build_statistics(window(points[:31]), cursor, "HOURLY")
        second, _ = build_statistics(window(points[31:]), cursor, "HOURLY")
        assert [(r["start"], r["state"], r["sum"]) for r in first + second] == [
            (r["start"], pytest.approx(r["state"]), pytest.approx(r["sum"])) for r in whole
        ]

    def test_hours_before_emit_from_are_priming_only(self):
        """A re-import fetches an hour early so its first hour is complete."""
        points = hourly_points(ADELAIDE, WINTER, 24)
        first_utc = utc_hour(datetime.fromisoformat(points[0]["startDate"]))
        cursor = _ImportCursor(consumption_sum=50.0, emit_from=first_utc + HOUR)
        consumption, _ = build_statistics(window(points), cursor, "HOURLY")

        assert consumption[0]["start"] == first_utc + HOUR
        # Complete: the second half of point 0 plus the first half of point 1.
        expected = (points[0]["totalConsumedEnergy"] + points[1]["totalConsumedEnergy"]) / 2000
        assert consumption[0]["state"] == pytest.approx(expected)
        # The priming hour added nothing to the running total.
        assert consumption[0]["sum"] == pytest.approx(50.0 + expected)

    def test_a_missing_point_does_not_hold_up_later_hours(self):
        points = hourly_points(ADELAIDE, WINTER, 10)
        del points[4]
        cursor = _ImportCursor()
        consumption, _ = build_statistics(window(points), cursor, "HOURLY")
        written = sum(row["state"] for row in consumption)
        assert written + pending_kwh(cursor) == pytest.approx(total_kwh(points))
        assert len(cursor.pending) == 1

    def test_sums_never_decrease(self):
        consumption, cost = build_statistics(
            window(hourly_points(ADELAIDE, DST_START, 72)), _ImportCursor(), "HOURLY"
        )
        for rows in (consumption, cost):
            assert all(b["sum"] >= a["sum"] for a, b in pairwise(rows))
