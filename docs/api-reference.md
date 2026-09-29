# Origin Energy GraphQL API

Everything here was measured against a live account on 2026-07-23, not
inferred from documentation. Origin runs [Kraken][kraken] (Octopus
Energy's licensed platform) behind a gateway at:

```
POST https://api.rx.originenergy.com.au/v1/gateway/graphql
Authorization: Bearer <auth0 access token>
```

**Introspection is enabled**, so the schema is self-describing: 121
object types, 73 enums, 12 interfaces, 13 mutations. `tools/explore_schema.py`
dumps the whole thing. Gateway build reports `environment: prod`,
`version: 1.0.0.3353`.

## Shape

`Query.viewer` splits into two worlds:

- `viewer.kraken` — energy accounts, meters, usage, bills, tariffs. This
  is the useful one.
- `viewer.digital` — Origin's own service catalogue across product lines
  beyond energy (`DigitalServiceType` covers `BROADBAND`, `LPG`, `OHM`,
  `VPP_BATTERY`, `EV_CHARGER`, `SOLAR`, …). Thin: ids, types, statuses.

Services are a union of `ElectricityService`, `GasService`,
`EmbeddedHotWaterService` and `EmbeddedElectricityService`. The first two
carry `usage`; all four carry `product`, `meterPoint` and `property`.

## What can be read

### Usage — `service.usage(timeUnit, startDate, endDate, orderBy, filter)`

Returns an `EnergyUsage` envelope, not a bare list. It is tempting to
reach straight past it for `dataPoints`, but the envelope is where the
metadata that saves you guessing lives:

| field | value seen | why it matters |
|---|---|---|
| `unitsOfMeasure` | `totalConsumedEnergy: WATT_HOUR`, `totalCost: CENT` | units are declared, so Wh→kWh and cents→dollars need not be hard-coded |
| `lastReadingDate` | typically yesterday, in UTC | exact freshness watermark; better than guessing an overlap window |
| `hasMissingDataPoints` / `hasMissingCosts` | `true` / `false` | gaps flagged without scanning points |
| `hasFeedIn` | `false` | whether solar export exists at all |
| `timezone` | `Australia/Sydney` | authoritative, per service |
| `hasPrevious` / `hasNext` | see caveat | unreliable outside the data range — both go `false` for windows entirely before data starts, so don't use them to discover the start |

Each `EnergyUsageDataPoint` carries `type` (`UsageDataType`: `ACTUAL`,
`APPROXIMATE`, `PARTIAL`, `PREDICTED`, `MISSING`), `startDate`/`endDate`
as `DateTimeInZone` (local offset, not UTC), plus `totalConsumedEnergy`,
`totalFeedInEnergy`, `totalCost`, `usageCost`, `supplyCost`,
`totalEarnings`, `feedInEarned`.

Only `ACTUAL` and `MISSING` were observed. `MISSING` points are returned
as full buckets with every value null — they pad the window out past
`lastReadingDate` rather than being omitted. A typical trailing week is
`{ACTUAL: 147, MISSING: 21}`.

`endDate` is inclusive: an hourly bucket ends `…T21:59:59.999`, a daily
one `…T23:59:59.999`. Consecutive query windows therefore overlap by one
boundary point and must be de-duplicated on `startDate`.

### Granularity — `UsageTimeUnit`

| unit | bucket | alignment |
|---|---|---|
| `HALF_HOURLY` | 30 min | **anchored to the request time**, not the clock: a query at 20:23 yields 20:23–20:52 buckets |
| `HOURLY` | 60 min | snapped to the clock hour ✓ |
| `DAILY` | 24 h | local midnight |
| `MONTHLY` | calendar month | snaps to the 1st |
| `BILLING` | billing period | **returns exactly 1 point** regardless of range — the current period only |

`HOURLY` is the one that drops into Home Assistant's statistics
unchanged. `HALF_HOURLY` would need re-bucketing to gain nothing HA can
store, since external statistics are hourly.

`availableUsageTimeUnits` under-reports: it advertises
`[BILLING, MONTHLY, DAILY, HOURLY]`, yet `HALF_HOURLY` returns 336 points
over 7 days without complaint. Treat it as a lower bound — but note the
integration deliberately sticks to what is advertised, since a service
that genuinely lacks a granularity is indistinguishable from one that
merely under-reports it.

### Accounts, bills, tariffs, meters

- `Account` — `balance` and `balanceInfo` (cents, negative = credit),
  `status`, `accountState`, `nextBillDate`,
  `currentBillingPeriodStartDate`, `dueCharges`, `ledgers`,
  `isInHardship`, `hasHeldBill`, `lifeSupport`.
- `Account.bills` — Relay connection of `KrakenBill`: `fromDate`,
  `toDate`, `issuedDate`, `paymentDueDate`, `billType`
  (`STATEMENT`/`INVOICE`/`CREDIT_NOTE`/`PRE_KRAKEN`), `estimatedStatus`,
  `isFinal`. No line items and no PDF link.
- `product.rates` — full tariff: `rateIncludingTax` / `rateExcludingTax`,
  `timeOfDay` (`PEAK`/`SHOULDER`/`OFFPEAK`/`ALLDAY`), `bandCategory`
  (`CONSUMPTION_CHARGE`/`STANDING_CHARGE`), `networkTariffCode`,
  `stepStart`/`stepEnd` for block tariffs, `activeFrom`/`activeTo`.
  Only currently-active rates came back even with no filter, so
  historical tariffs aren't reconstructable.
- `meterPoint` — NMI, `supplyStatus`, distributor name and emergency
  number, and every meter ever fitted with `activeFrom`/`activeTo`.
- `meter.registers` — `energyDirection` (`IMPORT`/`EXPORT`),
  `networkTariffCode`, `timeOfDay`, plus `readings` as a Relay
  connection of billing reads (`value`, `readAt`, `ReadingType`). An
  `EXPORT` register is how you detect solar.
- `Property` — address components including `timezone` and
  `coordinates`, `moveInDate`/`moveOutDate`.
- `Query.serviceFees` and `service.fees` — connection/disconnection fee
  schedules.
- `viewer.kraken.concessions` — concession card status.

### Bulk export — `service.usageReportJob`

Returns `{status, progress, downloadUrl}` where `downloadUrl` is a signed
CloudFront link to a half-hourly CSV. Reading it is free, but it only
reports a job that already exists; generating a new one needs the
`cadUsageReport` **mutation**, which takes a `recipientAddress` and
emails the result. Not a viable polling path, and the CSV carries no cost
columns.

## Limitations

### Range limits differ by granularity

Both caps apply to every granularity; which one you hit first depends on
the bucket size, and they report themselves completely differently:

| cap | applies | fails with |
|---|---|---|
| **9 elapsed days** | sub-daily only | `EXTERNAL_API_ERROR`, "There was an unexpected error" |
| **1000 data points** | all | `USER_INPUT_ERROR`, "Range is too large…" |

The sub-daily cap counts *days*, not points:

```
HOURLY       9d ->  216 points OK      HALF_HOURLY  9d ->  432 points OK
HOURLY      10d ->  EXTERNAL_API_ERROR HALF_HOURLY 10d ->  EXTERNAL_API_ERROR
HOURLY      60d ->  USER_INPUT_ERROR   DAILY     1000d -> 1000 points OK
```

240 points fails where 432 succeeds, so it cannot be counting points. Push
an `HOURLY` window far enough (60 days, 1440 points) and the point ceiling
takes over and you get the *validation* error instead — which is how you
can tell the two apart. `DAILY` accepts exactly 1000 points and rejects
1200.

Only the point ceiling is documented; the day cap is not, and it arrives
disguised as a server error. See below.

Consequence: an hourly backfill needs one query per week. Two years would
be ~105 sequential requests at ~2s each.

### `filter: null` crashes the backend

```graphql
usage(..., filter: null)          # INTERNAL_SERVER_ERROR, every time
usage(...)                        # fine
usage(..., filter: {type_in: [ACTUAL]})  # fine
```

Reproducible on demand, not intermittent. The resolver evidently
dereferences the filter without a null check. This is a real trap because
most GraphQL clients serialise an unset optional argument as exactly this
null — the argument must be **omitted from the variables map**, not passed
as null. Declaring `$filter` in the query and leaving it out of the
variables is fine.

The integration sidesteps this by not declaring the argument at all;
`MISSING` points are dropped client-side instead.

### A malformed query returns `INTERNAL_SERVER_ERROR`, not a parse error

This is the single most misleading behaviour of the gateway. Send a
document with an unbalanced brace and it answers:

```json
{"errors": [{"message": "There was an unexpected error",
             "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]}
```

No `GRAPHQL_PARSE_FAILED`, no line or column, nothing to distinguish a
typo in your query from an outage at Origin. It costs hours: a
hand-written probe that silently lost a `}` looks exactly like a backend
that has started rejecting valid work, and every conclusion drawn from it
is wrong.

Two habits make this survivable, both used by the scripts in `tools/`:
derive probe queries by editing a known-good document rather than
retyping one, and assert the braces balance before sending.

### `INTERNAL_SERVER_ERROR` is usually deterministic

The natural reading of a 500 is "transient, retry later". Here it usually
is not. Every occurrence traced during this work had a fixed cause —
`filter: null`, a sub-daily range over the day limit, or an unparsable
document — and each reproduced on demand.

Genuine flakiness was looked for and not found: **40 consecutive
identical 7-day HOURLY queries all succeeded**, on top of ~30 more during
other tests. So if this error appears, suspect the request before
suspecting Origin.

`api.py` still retries `INTERNAL_SERVER_ERROR`, `EXTERNAL_API_ERROR`,
HTTP 5xx, timeouts and transport errors three times with a linear
backoff. That is precaution for the ordinary hazards of a cloud API — a
dropped connection on a once-a-day poll would otherwise cost a full day
of data — not a workaround for a known fault. `USER_INPUT_ERROR` raises
immediately, since a range that is too large will be too large again.

### History is bounded by the agreement, not by retention

Data begins exactly at `service.validFrom` / `property.moveInDate` and
nothing exists before it. On the account used here, `validFrom` matched
the first CSV row and the first non-null API point to the second. Windows
before that return full buckets of `MISSING` rather than an error or an
empty list — at 120, 400 and 1000 days back, every point was `MISSING`.

So a backfill is bounded by `validFrom`, with `FIRST_RUN_HISTORY_DAYS`
left only as an upper cap. Asking for two years on a young account is
~95 pointless queries returning nothing.

Note there can be **more than one service** on an account. The test
account lists two, both `serviceState: ACTIVE` and both on the same NMI;
only the boolean `active` separates the live agreement from the defunct
one (whose `validFrom == validTo`). `serviceState` is no help. Picking
`services[0]` is therefore not safe — `pick_service()` filters on
`active` and falls back to the most recently valid agreement.

### Auth

Access tokens last one hour. Only `POST /usernamepassword/login` is
protected by Kasada, so a browser-imported `auth0` session cookie can be
exchanged silently via `/authorize?prompt=none` + PKCE indefinitely. See
[session-cookie.md](session-cookie.md).

### The request must look like it came from a browser

Origin fronts the auth domain with a WAF. `GET /authorize` answers
**HTTP 403** to a client that doesn't present browser headers, before
Auth0 ever sees the request — so a perfectly valid session cookie fails,
and the natural place to put the blame is the cookie.

This is a specific trap inside Home Assistant. Its
`async_create_clientsession()` accepts a `headers=` keyword, but then
replaces the session's defaults immediately afterwards:

```python
# homeassistant/helpers/aiohttp_client.py
clientsession._default_headers = MappingProxyType({USER_AGENT: SERVER_SOFTWARE})
```

Anything passed as `headers=` is therefore **silently discarded**, and
every request goes out as `HomeAssistant/<version> aiohttp/<version>`.
Headers have to be attached per request instead — which is what the
comment above that line tells you to do, and what `auth.py` and `api.py`
now do.

Worth knowing because it is invisible from outside: a probe script using a
plain `aiohttp.ClientSession(headers=…)` succeeds while the integration
fails on the identical account and cookie. `tests/test_api.py`
asserts the browser `User-Agent` reaches the request, since the failure
mode otherwise looks exactly like bad credentials.

Note the WAF rule is narrow. A plain `GET` of `/.well-known/jwks.json`
returns 200 under either identity; only `/authorize` cares.

## Mutations (not used)

The schema exposes 13, including `setPaymentMethod`, `setPaymentSchedule`,
`refund`, `balanceTransfer`, `moveOutSubmission`, `selfMeterRead` and
`cadUsageReport`. This integration is read-only and calls none of them.
Listed only so nobody assumes they were tested.

## Verification

`tools/verify_against_csv.py` reconciles the API against Origin's own CSV
export — download one from My Account and point the script at it. Over
the full history of the test account:

```
64 days compared, 0 outside +/-0.01 kWh
```

That covers unit conversion, day-boundary handling, HOURLY→daily rollup
and chunk-boundary de-duplication together, and it is the check worth
re-running after any change to the query or the import path.

The pytest suite under `tests/` covers the parts that never touch the
network: agreement selection, retry classification, request construction,
running sums, unit scaling, de-duplication and the daylight-saving fold.

[kraken]: https://kraken.tech/
