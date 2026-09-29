"""DataUpdateCoordinator for Origin Energy.

Origin/Kraken hands us clean usage totals with real calendar dates
attached (not a live incrementing meter), so rather than faking a
continuously-increasing sensor, usage and cost are written directly into
Home Assistant's long-term statistics as an external source. That's the
mechanism the Energy dashboard expects for exactly this shape of data,
and is also what core's own `opower` integration (utility billing data)
does -- this coordinator follows the same pattern.
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import (
    OriginEnergyApiClient,
    OriginEnergyApiError,
    UsageWindow,
    chunk_days,
    parse_api_datetime,
    pick_service,
)
from .auth import (
    OriginEnergyAuthError,
    OriginEnergySessionExpired,
    async_get_tokens,
    current_session_cookie,
    seed_session_cookie,
)
from .const import (
    CONF_AGREEMENT_ID,
    CONF_BACKFILL_COMPLETE,
    CONF_SESSION_COOKIE,
    CONF_TIME_UNIT,
    CONF_TIMEZONE,
    CONF_UPDATE_INTERVAL_HOURS,
    DEFAULT_TIME_UNIT,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
    FIRST_RUN_HISTORY_DAYS,
    MAX_UPDATE_INTERVAL_HOURS,
    MIN_UPDATE_INTERVAL_HOURS,
    OVERLAP_DAYS,
)

_LOGGER = logging.getLogger(__name__)

type OriginEnergyConfigEntry = ConfigEntry["OriginEnergyCoordinator"]

# Origin declares the units it is answering in rather than fixing them, so
# convert from what the API actually says. Writing a wrong-magnitude value
# into long-term statistics is painful to undo, so an unrecognised unit is
# treated as a hard failure rather than guessed at.
ENERGY_TO_KWH = {"WATT_HOUR": 0.001, "KILOWATT_HOUR": 1.0, "MEGAWATT_HOUR": 1000.0}
COST_TO_DOLLARS = {"CENT": 0.01, "DOLLAR": 1.0}

# How far back to look for the statistic preceding a re-imported window.
# Comfortably longer than OVERLAP_DAYS so a daily poll always finds it.
BASELINE_LOOKBACK_DAYS = 30

# Mint a new access token once the current one is within this of expiring.
# Tokens last an hour and a long backfill can outlive one, which would
# otherwise fail every remaining window with a 401.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


def configured_update_interval(config_entry: ConfigEntry) -> timedelta:
    """Poll interval from the entry's options, clamped to the sane range.

    Clamped rather than trusted: the value reaches here from a stored
    options dict, which survives downgrades and hand-edited
    .storage files, and a zero or negative interval would spin the
    coordinator against Origin's API as fast as the event loop allows.
    """
    hours = config_entry.options.get(
        CONF_UPDATE_INTERVAL_HOURS, DEFAULT_UPDATE_INTERVAL_HOURS
    )
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        hours = DEFAULT_UPDATE_INTERVAL_HOURS
    hours = min(max(hours, MIN_UPDATE_INTERVAL_HOURS), MAX_UPDATE_INTERVAL_HOURS)
    return timedelta(hours=hours)


def statistic_slug(value: str) -> str:
    """Reduce an identifier to the character set statistic IDs allow.

    Home Assistant validates external statistic IDs against
    ``[\\da-z_]+:[\\da-z_]+`` with no leading, trailing or doubled
    underscores, and rejects anything else outright. Origin's agreement IDs
    are plain digits on the accounts seen so far -- for which this is a
    no-op -- but nothing documents that, and a hyphen in someone else's ID
    would otherwise make every write raise.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "unknown"


@dataclass
class OriginEnergyData:
    """Small marker object returned to any entities watching the coordinator."""

    last_updated: datetime


HOUR = timedelta(hours=1)

# How long one data point covers, for the units that are finer than a day.
# These are spread over the UTC hours they overlap. Coarser units are filed
# whole under the UTC hour they start in: splitting a day's total across
# hours would invent a consumption profile Origin never reported.
SUB_DAILY_DURATIONS = {"HOURLY": HOUR, "HALF_HOURLY": timedelta(minutes=30)}


def utc_hour(instant: datetime) -> datetime:
    """The UTC clock hour containing `instant`."""
    return dt_util.as_utc(instant).replace(minute=0, second=0, microsecond=0)


def hour_shares(start: datetime, time_unit: str) -> list[tuple[datetime, timedelta, float]]:
    """Split one data point across the UTC hours it covers.

    Returns (hour, time covered in that hour, fraction of the point's value)
    for each hour. Home Assistant only checks that a statistic's start is on
    the hour *before* converting it to UTC, so Origin's 10:00+09:30 would be
    accepted and stored at 00:30 UTC, off the hourly grid every other row
    sits on. In South Australia and the Northern Territory every local hour
    straddles two UTC hours, so each point is shared between them in
    proportion to time.
    """
    duration = SUB_DAILY_DURATIONS.get(time_unit)
    if duration is None:
        return [(utc_hour(start), HOUR, 1.0)]

    begin = dt_util.as_utc(start)
    end = begin + duration
    shares = []
    hour = utc_hour(begin)
    while hour < end:
        covered = min(end, hour + HOUR) - max(begin, hour)
        shares.append((hour, covered, covered / duration))
        hour += HOUR
    return shares


@dataclass
class _HourBucket:
    """One UTC hour being assembled from the data points that overlap it."""

    consumption: float = 0.0
    cost: float = 0.0
    covered: timedelta = timedelta(0)


@dataclass
class _ImportCursor:
    """Running totals carried across query windows.

    Statistics store an absolute running sum per point, so an import has to
    walk forward in time accumulating totals. last_start also does the
    de-duplication: Origin's ranges are inclusive at both ends, so
    consecutive windows share a boundary point that must not be counted
    twice.

    pending holds UTC hours that have not been written yet because a point
    still to come may add to them. Hours before emit_from are assembled but
    never written: an import fetches from an hour early so the first hour it
    writes is complete, and that earlier hour is already stored.
    """

    consumption_sum: float = 0.0
    cost_sum: float = 0.0
    last_start: datetime | None = None
    written: int = 0
    emit_from: datetime | None = None
    pending: dict[datetime, _HourBucket] = field(default_factory=dict)


class OriginEnergyCoordinator(DataUpdateCoordinator[OriginEnergyData]):
    """Fetch usage from Origin Energy and feed it into HA statistics."""

    config_entry: OriginEnergyConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: OriginEnergyConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=configured_update_interval(config_entry),
        )
        # No headers= here: Home Assistant replaces a session's default
        # headers with its own User-Agent right after construction, so
        # anything set here is silently dropped. The browser identity Origin's
        # WAF insists on is attached per request instead, in auth.py and api.py.
        self._session = async_create_clientsession(hass)
        seed_session_cookie(self._session, config_entry.data[CONF_SESSION_COOKIE])
        self.agreement_id: str = config_entry.data[CONF_AGREEMENT_ID]
        self.timezone = ZoneInfo(config_entry.data[CONF_TIMEZONE])
        self.time_unit: str = config_entry.data.get(CONF_TIME_UNIT, DEFAULT_TIME_UNIT)

        id_prefix = statistic_slug(self.agreement_id)
        self.consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
        self.cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_cost"

        self._backfill_task: asyncio.Task[None] | None = None
        # Guards the statistics import so a background backfill and a
        # scheduled update can't interleave and corrupt the running sums.
        self._import_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None

    def async_adopt_update_interval(self) -> bool:
        """Take up a newly-configured poll interval. True if it changed.

        The caller needs the return value because this fires for *any*
        config entry write, and most of them are ours rather than the
        user's -- persisting a rolled session cookie, setting the
        backfill-complete flag. Reacting to those would re-anchor the
        poll schedule every time the cookie rotated.
        """
        interval = configured_update_interval(self.config_entry)
        if interval == self.update_interval:
            return False
        _LOGGER.debug(
            "Origin Energy poll interval changed from %s to %s",
            self.update_interval,
            interval,
        )
        self.update_interval = interval
        return True

    async def async_close(self) -> None:
        """Stop the backfill before the entry finishes unloading.

        The session is deliberately left alone. Home Assistant registers an
        unload callback for any session created during config entry setup
        and detaches it itself; closing it here would be closing a session
        we don't own, which it warns about.
        """
        if self._backfill_task is not None:
            self._backfill_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._backfill_task

    def _persist_rolled_cookie(self) -> None:
        """Save the session cookie back if Auth0 rolled its expiry forward.

        Auth0 re-issues the SSO cookie on each successful silent
        authorization, pushing its ~100-day expiry out again. Writing the new
        value back to the config entry is what lets a once-imported session
        renew itself indefinitely instead of hard-expiring.
        """
        cookie = current_session_cookie(self._session)
        if not cookie or cookie == self.config_entry.data[CONF_SESSION_COOKIE]:
            return
        _LOGGER.debug("Origin Energy session cookie refreshed, persisting")
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_SESSION_COOKIE: cookie},
        )

    async def _async_api(self) -> OriginEnergyApiClient:
        """Return a client holding a currently-valid access token.

        Tokens last an hour, which a full history backfill can outlive, so
        the token is cached and re-minted as it nears expiry rather than
        being fetched once per update. The imported session cookie in the
        jar is what makes the silent exchange work.
        """
        now = dt_util.utcnow()
        if (
            self._access_token is not None
            and self._token_expires_at is not None
            and self._token_expires_at - now > TOKEN_REFRESH_MARGIN
        ):
            return OriginEnergyApiClient(self._session, self._access_token)

        try:
            tokens = await async_get_tokens(self._session)
        except OriginEnergySessionExpired as err:
            # Only a browser sign-in can fix this -- prompt the user to
            # re-import rather than retrying on a schedule forever.
            raise ConfigEntryAuthFailed(str(err)) from err
        except OriginEnergyAuthError as err:
            raise UpdateFailed(str(err)) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Error connecting to Origin Energy: {err}") from err

        self._access_token = tokens.access_token
        self._token_expires_at = now + timedelta(seconds=tokens.expires_in)
        self._persist_rolled_cookie()
        return OriginEnergyApiClient(self._session, self._access_token)

    async def _async_update_data(self) -> OriginEnergyData:
        # Resolve auth up front so an expired session raises before any of
        # the import work, and surfaces as a re-auth prompt.
        await self._async_api()

        if self.config_entry.data.get(CONF_BACKFILL_COMPLETE):
            try:
                async with self._import_lock:
                    await self._async_import_recent()
            except OriginEnergyApiError as err:
                raise UpdateFailed(str(err)) from err
            except (aiohttp.ClientError, TimeoutError) as err:
                raise UpdateFailed(f"Error connecting to Origin Energy: {err}") from err
        else:
            # A first backfill is one query per week of history; running it
            # inline would block Home Assistant's startup for minutes on a
            # long-standing account.
            self._start_backfill()

        return OriginEnergyData(last_updated=dt_util.utcnow())

    # ------------------------------------------------------------------
    # History backfill
    # ------------------------------------------------------------------

    def _start_backfill(self) -> None:
        """Kick off the history import without holding up setup."""
        if self._backfill_task is not None and not self._backfill_task.done():
            return
        _LOGGER.debug("Starting Origin Energy history backfill in the background")
        self._backfill_task = self.config_entry.async_create_background_task(
            self.hass, self._async_backfill(), name=f"{DOMAIN} history backfill"
        )

    async def _async_backfill(self) -> None:
        """Import all available history, oldest first, then mark it done.

        Only on success is CONF_BACKFILL_COMPLETE set, so a run interrupted
        by a restart simply starts over next time rather than leaving a
        permanent hole. Re-importing is idempotent: statistics are keyed by
        start time and the sums are re-derived from the same origin.
        """
        try:
            async with self._import_lock:
                start = await self._async_history_start()
                _LOGGER.debug("Backfilling Origin Energy usage from %s", start)
                cursor = await self._async_import(
                    start, dt_util.now(self.timezone), _ImportCursor()
                )
        except asyncio.CancelledError:
            raise
        except ConfigEntryAuthFailed as err:
            # Nothing here can raise this into the coordinator's own
            # machinery, so the re-auth flow has to be started by hand.
            _LOGGER.warning("Origin Energy backfill needs re-authentication: %s", err)
            self.config_entry.async_start_reauth(self.hass)
            return
        except Exception as err:  # noqa: BLE001 - retried on the next update
            _LOGGER.warning(
                "Origin Energy history backfill failed, will retry: %s", err
            )
            return

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_BACKFILL_COMPLETE: True},
        )
        _LOGGER.info(
            "Origin Energy history backfill complete, %d points imported",
            cursor.written,
        )

    async def _async_history_start(self) -> datetime:
        """Earliest instant worth asking for.

        Usage data begins when the agreement does, and earlier windows come
        back as buckets of MISSING points rather than as an error, so
        validFrom is the real floor. FIRST_RUN_HISTORY_DAYS only caps
        accounts that have been running longer than we care to import.
        """
        cap = dt_util.now(self.timezone) - timedelta(days=FIRST_RUN_HISTORY_DAYS)
        try:
            api = await self._async_api()
            services = await api.async_get_services()
        except OriginEnergyApiError as err:
            _LOGGER.debug("Could not read service validity (%s), using cap", err)
            return cap

        service = pick_service(services, self.agreement_id)
        valid_from = parse_api_datetime((service or {}).get("validFrom"))
        if valid_from is None:
            return cap
        return max(valid_from, cap)

    # ------------------------------------------------------------------
    # Incremental update
    # ------------------------------------------------------------------

    async def _async_import_recent(self) -> None:
        """Extend the statistics with whatever has been published since."""
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, self.consumption_statistic_id, True, set()
        )
        if not last_stat:
            # The backfill was marked complete but produced nothing (a brand
            # new agreement with no readings yet). Try again from scratch.
            _LOGGER.debug("No Origin Energy statistics yet, re-running backfill")
            self._start_backfill()
            return

        last_point = dt_util.utc_from_timestamp(
            last_stat[self.consumption_statistic_id][0]["start"]
        )

        # Ask the API where real data currently ends. Anchoring the overlap
        # to that watermark rather than to wall-clock now means late-arriving
        # data still gets covered.
        now = dt_util.now(self.timezone)
        api = await self._async_api()
        probe = await api.async_get_usage(
            self.agreement_id, now - timedelta(days=2), now, self.time_unit
        )
        last_reading = probe.last_reading_date
        if last_reading is not None:
            _LOGGER.debug(
                "Origin Energy data available to %s, imported to %s",
                last_reading,
                last_point,
            )

        anchor = min(last_point, last_reading) if last_reading else last_point
        # On the UTC hour, so the baseline (everything before start) and the
        # first hour written meet exactly.
        start = utc_hour(anchor - timedelta(days=OVERLAP_DAYS))
        end = max(last_reading or now, last_point) + timedelta(minutes=1)

        cursor = await self._async_baseline_cursor(start)
        if cursor is None:
            # Couldn't establish what the sums were before the overlap
            # window, so re-deriving them would corrupt the series. Fall
            # back to appending only what is genuinely new.
            _LOGGER.debug("No baseline before %s, appending new points only", start)
            consumption_sum, cost_sum = await self._async_sums_at(last_point)
            start = last_point + HOUR
            cursor = _ImportCursor(consumption_sum, cost_sum, emit_from=start)

        cursor = await self._async_import(start, end, cursor)
        if not cursor.written:
            _LOGGER.debug("No new Origin Energy usage data since last update")

    async def _async_baseline_cursor(self, start: datetime) -> _ImportCursor | None:
        """Sums as of the last statistic strictly before `start`.

        Re-importing a window means recomputing every sum from there
        forward, which has to continue from whatever preceded it. Returns
        None when no preceding statistic can be found, since assuming zero
        would silently reset the series.
        """
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start - timedelta(days=BASELINE_LOOKBACK_DAYS),
            start,
            {self.consumption_statistic_id, self.cost_statistic_id},
            "hour",
            None,
            {"sum"},
        )
        consumption_rows = rows.get(self.consumption_statistic_id) or []
        if not consumption_rows:
            return None

        cost_rows = rows.get(self.cost_statistic_id) or []
        return _ImportCursor(
            consumption_sum=float(consumption_rows[-1]["sum"]),
            cost_sum=float(cost_rows[-1]["sum"]) if cost_rows else 0.0,
            last_start=None,
        )

    async def _async_sums_at(self, instant: datetime) -> tuple[float, float]:
        """Running sums at a known-present statistic."""
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            instant,
            instant + timedelta(seconds=1),
            {self.consumption_statistic_id, self.cost_statistic_id},
            "hour",
            None,
            {"sum"},
        )

        def _sum(statistic_id: str) -> float:
            entries = rows.get(statistic_id) or []
            return float(entries[0]["sum"]) if entries else 0.0

        return _sum(self.consumption_statistic_id), _sum(self.cost_statistic_id)

    # ------------------------------------------------------------------
    # Shared import machinery
    # ------------------------------------------------------------------

    def _statistic_metadata(self) -> tuple[StatisticMetaData, StatisticMetaData]:
        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Origin Energy consumption",
            source=DOMAIN,
            statistic_id=self.consumption_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        cost_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Origin Energy cost",
            source=DOMAIN,
            statistic_id=self.cost_statistic_id,
            unit_class=None,
            unit_of_measurement=None,
        )
        return consumption_metadata, cost_metadata

    async def _async_import(
        self,
        start: datetime,
        end: datetime,
        cursor: _ImportCursor,
    ) -> _ImportCursor:
        """Fetch `start`..`end` in windows, writing statistics as they land.

        Origin caps a single query at a few days for sub-daily granularity,
        so long ranges are split. Writing each window as it arrives keeps
        memory flat over a multi-year backfill and lets the Energy dashboard
        fill in progressively.

        The first hour written is the UTC hour containing `start`. Fetching
        starts an hour before that: in a half-hour timezone the point that
        began half an hour earlier supplies the first half of that hour.
        """
        consumption_metadata, cost_metadata = self._statistic_metadata()
        window = timedelta(days=chunk_days(self.time_unit))
        start = utc_hour(start)
        if cursor.emit_from is None:
            cursor.emit_from = start
        window_start = start - HOUR

        while window_start < end:
            window_end = min(window_start + window, end)
            # Re-resolved per window: a long backfill outlives one token.
            api = await self._async_api()
            result = await api.async_get_usage(
                self.agreement_id, window_start, window_end, self.time_unit
            )

            consumption_stats, cost_stats = build_statistics(
                result, cursor, self.time_unit
            )
            if consumption_stats:
                async_add_external_statistics(
                    self.hass, consumption_metadata, consumption_stats
                )
                async_add_external_statistics(self.hass, cost_metadata, cost_stats)
                cursor.written += len(consumption_stats)

            window_start = window_end

        return cursor


def build_statistics(
    result: UsageWindow, cursor: _ImportCursor, time_unit: str
) -> tuple[list[StatisticData], list[StatisticData]]:
    """Turn one window's data points into statistics rows on UTC hours.

    Advances the cursor's running sums, which is why points must arrive in
    chronological order. An hour is written once it is complete: either
    fully covered, or a later point has started after it ends (so a missing
    point stays missing rather than blocking everything after it). The last
    partly covered hour stays in the cursor for the next window, or for the
    next import, which re-fetches it.
    """
    energy_scale = _scale(ENERGY_TO_KWH, result.consumption_unit, "WATT_HOUR", "energy")
    cost_scale = _scale(COST_TO_DOLLARS, result.cost_unit, "CENT", "cost")

    consumption_stats: list[StatisticData] = []
    cost_stats: list[StatisticData] = []

    usable: list[tuple[datetime, dict]] = []
    for point in result.points:
        if point.get("totalConsumedEnergy") is None:
            # A MISSING bucket: either past lastReadingDate or a real
            # gap in the meter data. Both are padded out as full
            # buckets of nulls rather than being omitted.
            continue
        start = dt_util.parse_datetime(point["startDate"])
        if start is None:
            continue
        usable.append((start, point))

    # The gateway is asked for START_DATE_ASC and honours it, but the
    # running sums below are only correct in chronological order, and
    # ISO strings stop sorting chronologically at the end of daylight
    # saving: 02:00+11:00 and the 02:00+10:00 an hour later compare the
    # wrong way round as text. Sorting the parsed instants costs
    # nothing and removes the dependency entirely.
    usable.sort(key=lambda item: item[0])

    def emit_ready(now: datetime | None) -> None:
        """Write out, in order, every leading hour that nothing can add to."""
        while cursor.pending:
            hour = min(cursor.pending)
            bucket = cursor.pending[hour]
            finished = bucket.covered >= HOUR or (now is not None and hour + HOUR <= now)
            if not finished:
                return
            del cursor.pending[hour]
            if cursor.emit_from is not None and hour < cursor.emit_from:
                continue
            cursor.consumption_sum += bucket.consumption
            cursor.cost_sum += bucket.cost
            consumption_stats.append(
                StatisticData(start=hour, state=bucket.consumption, sum=cursor.consumption_sum)
            )
            cost_stats.append(
                StatisticData(start=hour, state=bucket.cost, sum=cursor.cost_sum)
            )

    for start, point in usable:
        # Skips both the boundary point shared with the previous window
        # and anything already imported.
        if cursor.last_start is not None and start <= cursor.last_start:
            continue
        cursor.last_start = start

        consumption_kwh = point["totalConsumedEnergy"] * energy_scale
        cost_dollars = (point.get("totalCost") or 0) * cost_scale

        emit_ready(dt_util.as_utc(start))
        for hour, covered, fraction in hour_shares(start, time_unit):
            bucket = cursor.pending.setdefault(hour, _HourBucket())
            bucket.consumption += consumption_kwh * fraction
            bucket.cost += cost_dollars * fraction
            bucket.covered += covered
        emit_ready(None)

    return consumption_stats, cost_stats


def _scale(table: dict[str, float], unit: str | None, default: str, label: str) -> float:
    """Look up a unit conversion, refusing to guess at an unknown one."""
    resolved = unit or default
    if resolved not in table:
        raise UpdateFailed(f"Unexpected {label} unit from Origin Energy: {unit}")
    return table[resolved]
