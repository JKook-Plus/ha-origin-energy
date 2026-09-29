"""Offline tests for the API client's decision-making.

These are the bits that are easy to get subtly wrong and hard to notice in
production: which agreement gets picked when an account has more than one,
and which failures are retried versus surfaced. No network is involved; the
session is faked.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import aiohttp
import pytest

from custom_components.origin_energy import api as api_module
from custom_components.origin_energy.api import (
    OriginEnergyApiClient,
    OriginEnergyApiError,
    chunk_days,
    format_api_datetime,
    pick_service,
    pick_time_unit,
)
from custom_components.origin_energy.const import BROWSER_HEADERS

SYDNEY_OFFSET = timezone(timedelta(hours=10))


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch):
    """Retries are exercised for their branching, not their timing."""
    monkeypatch.setattr(api_module, "RETRY_BACKOFF_SECONDS", 0)


class _FakeCtx:
    """Stands in for aiohttp's request context manager."""

    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def __aexit__(self, *exc):
        return False


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class FakeSession:
    """Replays a scripted list of responses, counting the attempts."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0
        self.sent = []
        self.headers_sent = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        self.sent.append(json)
        self.headers_sent.append(headers or {})
        return _FakeCtx(self._results.pop(0))


def graphql_error(code):
    return _FakeResponse(200, {"errors": [{"message": "boom", "extensions": {"code": code}}]})


def usage_ok(points=1):
    return _FakeResponse(
        200,
        {
            "data": {
                "viewer": {
                    "kraken": {
                        "service": {
                            "usage": {
                                "lastReadingDate": "2026-07-22T14:00:00.000Z",
                                "hasFeedIn": False,
                                "hasMissingDataPoints": True,
                                "unitsOfMeasure": {
                                    "totalConsumedEnergy": "WATT_HOUR",
                                    "totalCost": "CENT",
                                },
                                "dataPoints": [
                                    {
                                        "type": "ACTUAL",
                                        "startDate": "2026-07-22T23:00:00.000+10:00",
                                        "totalConsumedEnergy": 493,
                                        "totalCost": 22.143,
                                    }
                                ]
                                * points,
                            }
                        }
                    }
                }
            }
        },
    )


async def fetch(session):
    client = OriginEnergyApiClient(session, "token")
    now = datetime.now(UTC)
    return await client.async_get_usage("A-1", now - timedelta(days=1), now)


# ---------------------------------------------------------------- pick_service

LIVE = {"agreementId": "live", "active": True, "validFrom": "2026-05-19T14:00:00.000Z"}
CLOSED = {"agreementId": "old", "active": False, "validFrom": "2026-05-19T14:00:00.000Z"}
OLDER = {"agreementId": "older", "active": False, "validFrom": "2020-01-01T00:00:00.000Z"}


def test_pick_service_prefers_the_active_agreement():
    # The defunct agreement listed first is exactly the case services[0] got wrong.
    assert pick_service([CLOSED, LIVE])["agreementId"] == "live"


def test_pick_service_empty_list_yields_none():
    assert pick_service([]) is None


def test_pick_service_pins_to_a_configured_agreement():
    assert pick_service([CLOSED, LIVE], "old")["agreementId"] == "old"


def test_pick_service_unknown_agreement_falls_back_to_active():
    assert pick_service([CLOSED, LIVE], "gone")["agreementId"] == "live"


def test_pick_service_all_inactive_picks_most_recent():
    assert pick_service([OLDER, CLOSED])["agreementId"] == "old"


# ------------------------------------------------------------------ chunk_days

@pytest.mark.parametrize(
    ("unit", "days"),
    [
        ("HOURLY", 7),  # stays under the 9-day cap
        ("HALF_HOURLY", 7),  # matches hourly
        ("DAILY", 900),  # uses the point ceiling
        ("WEEKLY", 7),  # unknown unit is conservative
    ],
)
def test_chunk_days(unit, days):
    assert chunk_days(unit) == days


# -------------------------------------------------------------- pick_time_unit

@pytest.mark.parametrize(
    ("available", "unit"),
    [
        (["BILLING", "DAILY", "HOURLY"], "HOURLY"),
        (["BILLING", "DAILY"], "DAILY"),
        (None, "DAILY"),
    ],
)
def test_pick_time_unit(available, unit):
    assert pick_time_unit(available) == unit


# --------------------------------------------------------- format_api_datetime

def test_format_converts_a_local_instant_to_utc():
    # The gateway's DateTime scalar is given a literal trailing Z, so the
    # components have to be UTC. Formatting a local-zone instant directly
    # would shift the whole query window by the offset.
    assert (
        format_api_datetime(datetime(2026, 7, 23, 9, 0, tzinfo=SYDNEY_OFFSET))
        == "2026-07-22T23:00:00.000Z"
    )


def test_format_leaves_a_utc_instant_alone():
    assert (
        format_api_datetime(datetime(2026, 7, 23, 9, 0, tzinfo=UTC))
        == "2026-07-23T09:00:00.000Z"
    )


# ---------------------------------------------------------- query construction

@pytest.fixture
async def sent_request():
    session = FakeSession([usage_ok()])
    client = OriginEnergyApiClient(session, "token")
    await client.async_get_usage(
        "A-1",
        datetime(2026, 7, 20, 0, 0, tzinfo=SYDNEY_OFFSET),
        datetime(2026, 7, 27, 0, 0, tzinfo=SYDNEY_OFFSET),
    )
    return session.sent[0], session.headers_sent[0]


async def test_start_is_sent_as_utc(sent_request):
    body, _ = sent_request
    assert body["variables"]["startDate"] == "2026-07-19T14:00:00.000Z"


async def test_no_filter_variable_is_sent(sent_request):
    # An explicit filter: null is the one input that reliably 500s this
    # backend, and it is exactly what a client sends for an unset optional.
    body, _ = sent_request
    assert "filter" not in body["variables"]


async def test_no_filter_argument_is_declared(sent_request):
    body, _ = sent_request
    assert "filter" not in body["query"]


async def test_query_braces_are_balanced(sent_request):
    body, _ = sent_request
    assert sum((c == "{") - (c == "}") for c in body["query"]) == 0


# Home Assistant overwrites a session's default headers with its own
# User-Agent after construction, so anything set at session level is
# silently dropped -- and Origin's WAF answers non-browser clients with
# HTTP 403. The headers have to be on each request.

async def test_sends_a_user_agent(sent_request):
    _, headers = sent_request
    assert "User-Agent" in headers


async def test_user_agent_is_the_browser_one(sent_request):
    _, headers = sent_request
    assert headers["User-Agent"] == BROWSER_HEADERS["User-Agent"]


async def test_does_not_identify_as_home_assistant(sent_request):
    _, headers = sent_request
    assert "HomeAssistant" not in headers["User-Agent"]


async def test_still_carries_the_bearer_token(sent_request):
    _, headers = sent_request
    assert headers["Authorization"] == "Bearer token"


@pytest.mark.parametrize("header", ["Origin", "Referer", "sec-ch-ua"])
async def test_sends_browser_header(sent_request, header):
    _, headers = sent_request
    assert headers.get(header) == BROWSER_HEADERS[header]


# ---------------------------------------------------------------- retry policy

async def test_retries_internal_server_error():
    session = FakeSession([graphql_error("INTERNAL_SERVER_ERROR"), usage_ok()])
    await fetch(session)
    assert session.calls == 2


async def test_returns_data_after_retry():
    session = FakeSession([graphql_error("INTERNAL_SERVER_ERROR"), usage_ok()])
    result = await fetch(session)
    assert len(result.points) == 1


async def test_retries_external_api_error_twice():
    session = FakeSession([graphql_error("EXTERNAL_API_ERROR")] * 2 + [usage_ok()])
    await fetch(session)
    assert session.calls == 3


async def test_user_input_error_is_not_retried():
    session = FakeSession([graphql_error("USER_INPUT_ERROR")])
    with pytest.raises(OriginEnergyApiError):
        await fetch(session)
    assert session.calls == 1


async def test_gives_up_after_request_attempts():
    session = FakeSession([graphql_error("INTERNAL_SERVER_ERROR")] * 3)
    with pytest.raises(OriginEnergyApiError):
        await fetch(session)
    assert session.calls == 3


async def test_retries_transport_errors():
    session = FakeSession([aiohttp.ClientError("reset"), usage_ok()])
    await fetch(session)
    assert session.calls == 2


async def test_retries_http_5xx():
    session = FakeSession([_FakeResponse(503, {}), usage_ok()])
    await fetch(session)
    assert session.calls == 2


async def test_does_not_retry_http_401():
    session = FakeSession([_FakeResponse(401, {})])
    with pytest.raises(OriginEnergyApiError):
        await fetch(session)
    assert session.calls == 1


async def test_retries_timeouts():
    session = FakeSession([TimeoutError("slow"), usage_ok()])
    await fetch(session)
    assert session.calls == 2


# ------------------------------------------------------------ envelope parsing

@pytest.fixture
async def envelope():
    return await fetch(FakeSession([usage_ok(points=3)]))


async def test_reads_the_declared_energy_unit(envelope):
    assert envelope.consumption_unit == "WATT_HOUR"


async def test_reads_the_declared_cost_unit(envelope):
    assert envelope.cost_unit == "CENT"


async def test_parses_last_reading_date(envelope):
    assert envelope.last_reading_date.isoformat() == "2026-07-22T14:00:00+00:00"


async def test_carries_has_feed_in(envelope):
    assert envelope.has_feed_in is False


async def test_carries_has_missing_data_points(envelope):
    assert envelope.has_missing_points is True


async def test_keeps_every_data_point(envelope):
    assert len(envelope.points) == 3
