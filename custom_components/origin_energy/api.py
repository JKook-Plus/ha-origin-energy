"""Thin GraphQL client for Origin Energy's Kraken-backed gateway."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import ClientSession

from .const import (
    BROWSER_HEADERS,
    DEFAULT_QUERY_DAYS,
    DEFAULT_TIME_UNIT,
    GRAPHQL_URL,
    MAX_QUERY_DAYS,
    PREFERRED_TIME_UNITS,
    REQUEST_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

# Trimmed down from the web app's own captured query to just the fields
# we use. The gateway accepts arbitrary ad-hoc queries (the real
# AccountDetailsQuery sent by the browser is ~5KB), not a fixed/persisted
# query allowlist, so a smaller query is fine.
ACCOUNT_DETAILS_QUERY = """
query AccountDetailsQuery($type: ServiceType!) {
  viewer {
    kraken {
      services(type: $type) {
        ... on ElectricityService {
          agreementId
          type
          active
          validFrom
          validTo
          serviceState
          availableUsageTimeUnits
          meterPoint {
            identifier
          }
          property {
            address {
              components {
                timezone
              }
            }
          }
        }
        ... on GasService {
          agreementId
          type
          active
          validFrom
          validTo
          serviceState
          availableUsageTimeUnits
          meterPoint {
            identifier
          }
        }
      }
    }
  }
}
"""

# The `usage` field returns an envelope, not a bare list. lastReadingDate
# is the authoritative watermark for how far real data extends, and
# unitsOfMeasure spares us from assuming Wh/cents.
#
# Note the deliberate absence of a `filter` argument: the backend accepts
# a real EnergyUsageFilter but hard-fails on an explicit `filter: null`,
# which is what most clients send for an unset optional. Omitting it
# entirely is the only safe way to not filter.
ACCOUNT_USAGE_QUERY = """
query AccountUsageQuery($agreementId: String!, $type: ServiceType!, $timeUnit: UsageTimeUnit!, $startDate: DateTime!, $endDate: DateTime!) {
  viewer {
    kraken {
      service(type: $type, agreementId: $agreementId) {
        ... on ElectricityService {
          usage(timeUnit: $timeUnit, startDate: $startDate, endDate: $endDate, orderBy: START_DATE_ASC) {
            lastReadingDate
            hasFeedIn
            hasMissingDataPoints
            unitsOfMeasure {
              totalConsumedEnergy
              totalCost
            }
            dataPoints {
              type
              startDate
              endDate
              totalConsumedEnergy
              totalCost
              usageCost
              supplyCost
              totalFeedInEnergy
              totalEarnings
            }
          }
        }
        ... on GasService {
          usage(timeUnit: $timeUnit, startDate: $startDate, endDate: $endDate, orderBy: START_DATE_ASC) {
            lastReadingDate
            hasFeedIn
            hasMissingDataPoints
            unitsOfMeasure {
              totalConsumedEnergy
              totalCost
            }
            dataPoints {
              type
              startDate
              endDate
              totalConsumedEnergy
              totalCost
              usageCost
              supplyCost
            }
          }
        }
      }
    }
  }
}
"""

# GraphQL error codes worth another attempt. USER_INPUT_ERROR is
# deliberately absent: a range that's too large will be too large again.
#
# Note these two are not proof of a transient fault -- the gateway also
# returns them for several deterministic conditions, including a range
# over the sub-daily day limit, an explicit `filter: null`, and (unhelpfully)
# any GraphQL document it cannot parse. Retrying those simply costs three
# attempts before the error surfaces, which is the right trade for a
# once-a-day poll.
RETRYABLE_ERROR_CODES = frozenset({"INTERNAL_SERVER_ERROR", "EXTERNAL_API_ERROR"})

# The gateway is slow rather than unresponsive when it struggles, so this is
# generous. Without it aiohttp would allow 5 minutes per attempt, and three
# attempts could hang an update for a quarter of an hour.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)


class OriginEnergyApiError(Exception):
    """Raised on transport or GraphQL-level errors."""


class _RetryableError(OriginEnergyApiError):
    """A failure that is worth another attempt.

    Private: callers only ever see OriginEnergyApiError, since by the time
    one escapes _graphql the retries are already spent.
    """


@dataclass(frozen=True)
class UsageWindow:
    """One usage query's data points plus the envelope Origin sends with them."""

    points: list[dict[str, Any]]
    last_reading_date: datetime | None
    consumption_unit: str | None
    cost_unit: str | None
    has_feed_in: bool
    has_missing_points: bool


def pick_time_unit(available: list[str] | None) -> str:
    """Choose the finest usage granularity a service actually supports.

    Smart meters offer HOURLY; a basic accumulation meter may only go down
    to DAILY, so we ask rather than assume. Note availableUsageTimeUnits
    under-reports -- HALF_HOURLY works on services that don't list it --
    so treat it as a lower bound.
    """
    offered = set(available or ())
    return next(
        (unit for unit in PREFERRED_TIME_UNITS if unit in offered), DEFAULT_TIME_UNIT
    )


def pick_service(
    services: list[dict[str, Any]], agreement_id: str | None = None
) -> dict[str, Any] | None:
    """Choose the live agreement from an account's services.

    An account can carry several agreements for the same fuel: a closed or
    superseded one sits alongside the live one, told apart only by
    `active` (the defunct one also has validFrom == validTo). Taking
    services[0] picks whichever the gateway happened to list first, which
    is not necessarily the one still supplying power.

    Passing agreement_id pins the choice to an already-configured entry.
    """
    if not services:
        return None
    if agreement_id is not None:
        matched = next(
            (s for s in services if s.get("agreementId") == agreement_id), None
        )
        if matched is not None:
            return matched

    # Fall back to the most recently valid agreement so an account that has
    # just closed still resolves to its real meter rather than a stale one.
    pool = [s for s in services if s.get("active")] or services
    return max(pool, key=lambda s: s.get("validFrom") or "")


def chunk_days(time_unit: str) -> int:
    """Days per query that stay inside Origin's range limit.

    Sub-daily granularities are capped at a handful of days regardless of
    how few points that is, so an HOURLY backfill has to be split far more
    finely than a DAILY one. See MAX_QUERY_DAYS for the measurements.
    """
    return MAX_QUERY_DAYS.get(time_unit, DEFAULT_QUERY_DAYS)


def parse_api_datetime(value: str | None) -> datetime | None:
    """Parse one of Origin's timestamps, tolerating the trailing Z."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_api_datetime(value: datetime) -> str:
    """Render an instant the way the gateway's DateTime scalar expects.

    The literal trailing "Z" means the components have to *be* UTC, so
    convert first. Formatting a local-zone datetime directly would label
    e.g. 09:00+10:00 as "09:00Z" and silently shift the window by the
    offset -- ten hours, on the east coast.
    """
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _dig(data: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return default
        data = data.get(key)
    return default if data is None else data


class OriginEnergyApiClient:
    """Minimal GraphQL client for api.rx.originenergy.com.au."""

    def __init__(self, session: ClientSession, access_token: str) -> None:
        self._session = session
        # Sent per request, never as session defaults: Home Assistant
        # replaces a session's default headers with its own User-Agent after
        # construction, and Origin's WAF rejects anything that doesn't look
        # like a browser. See BROWSER_HEADERS.
        self._headers = {
            **BROWSER_HEADERS,
            "Authorization": f"Bearer {access_token}",
        }

    async def _attempt(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """One round trip, raising _RetryableError for transient failures."""
        try:
            async with self._session.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers=self._headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status >= 500:
                    raise _RetryableError(f"GraphQL server error: {resp.status}")
                if resp.status != 200:
                    raise OriginEnergyApiError(f"GraphQL request failed: {resp.status}")
                payload = await resp.json()
        except OriginEnergyApiError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            # TimeoutError covers both our own REQUEST_TIMEOUT and asyncio's,
            # neither of which is a ClientError.
            raise _RetryableError(f"Error connecting to Origin Energy: {err}") from err
        except Exception as err:  # noqa: BLE001 - surface as our own type
            raise OriginEnergyApiError(str(err)) from err

        if errors := payload.get("errors"):
            codes = {(err.get("extensions") or {}).get("code") for err in errors}
            if codes & RETRYABLE_ERROR_CODES:
                raise _RetryableError(str(errors))
            raise OriginEnergyApiError(str(errors))

        return payload["data"]

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run a query, retrying the gateway's intermittent server errors.

        The same query can fail once and succeed immediately afterwards, so
        a transient failure must not be allowed to sink a whole daily
        update. Deterministic errors (a range that's too large, a rejected
        token) raise on the first attempt.
        """
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            try:
                return await self._attempt(query, variables)
            except _RetryableError as err:
                if attempt == REQUEST_ATTEMPTS:
                    raise
                delay = RETRY_BACKOFF_SECONDS * attempt
                _LOGGER.debug(
                    "Origin Energy request failed (attempt %d/%d), retrying in %ds: %s",
                    attempt,
                    REQUEST_ATTEMPTS,
                    delay,
                    err,
                )
                await asyncio.sleep(delay)

        raise OriginEnergyApiError("Request failed")  # unreachable

    async def async_get_services(
        self, service_type: str = "ELECTRICITY"
    ) -> list[dict[str, Any]]:
        """Return the account's services of the given type.

        Each entry includes agreementId (needed for usage queries), the
        meter identifier (NMI), the property's IANA timezone, and the
        validity window that bounds how far back usage data exists.
        """
        data = await self._graphql(ACCOUNT_DETAILS_QUERY, {"type": service_type})
        return _dig(data, "viewer", "kraken", "services", default=[])

    async def async_get_usage(
        self,
        agreement_id: str,
        start_date: datetime,
        end_date: datetime,
        time_unit: str = "HOURLY",
        service_type: str = "ELECTRICITY",
    ) -> UsageWindow:
        """Return usage data points and metadata for one service agreement.

        HOURLY points come back snapped to the local clock hour, which is
        not a UTC hour in half-hour timezones; the coordinator re-buckets
        them. See PREFERRED_TIME_UNITS.
        """
        data = await self._graphql(
            ACCOUNT_USAGE_QUERY,
            {
                "agreementId": agreement_id,
                "type": service_type,
                "timeUnit": time_unit,
                "startDate": format_api_datetime(start_date),
                "endDate": format_api_datetime(end_date),
            },
        )
        usage = _dig(data, "viewer", "kraken", "service", "usage", default={})
        units = usage.get("unitsOfMeasure") or {}

        return UsageWindow(
            points=usage.get("dataPoints") or [],
            last_reading_date=parse_api_datetime(usage.get("lastReadingDate")),
            consumption_unit=units.get("totalConsumedEnergy"),
            cost_unit=units.get("totalCost"),
            has_feed_in=bool(usage.get("hasFeedIn")),
            has_missing_points=bool(usage.get("hasMissingDataPoints")),
        )
