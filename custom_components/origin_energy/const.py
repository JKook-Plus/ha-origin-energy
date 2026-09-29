"""Constants for the Origin Energy integration."""

DOMAIN = "origin_energy"

AUTH_DOMAIN = "id.originenergy.com.au"
APP_DOMAIN = "www.originenergy.com.au"
API_DOMAIN = "api.rx.originenergy.com.au"

# Public SPA client_id/audience used by originenergy.com.au's own "My
# Account" web app -- captured from its own network traffic. Not secret:
# it's a public OAuth client id, not a credential.
CLIENT_ID = "yOHRT97N3yH85jzTDlqN2A7Cf2D0cmQe"
AUDIENCE = "https://digitalapi"
REDIRECT_URI = f"https://{APP_DOMAIN}/auth/callback"
SCOPE = "openid profile email read:api all"

AUTHORIZE_URL = f"https://{AUTH_DOMAIN}/authorize"
TOKEN_URL = f"https://{AUTH_DOMAIN}/oauth/token"
GRAPHQL_URL = f"https://{API_DOMAIN}/v1/gateway/graphql"

# Origin fronts everything with a WAF that 403s obvious non-browser
# clients (e.g. aiohttp's default "Python/x aiohttp/x" User-Agent), so we
# present the same browser identity its own web app does. These are just
# the SPA's public request headers -- nothing account-specific.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": f"https://{APP_DOMAIN}",
    "Referer": f"https://{APP_DOMAIN}/",
}

# auth0-spa-js client-telemetry parameter the SPA puts on every silent
# /authorize call. Decodes to {"name":"auth0-spa-js","version":"1.10.0"}.
# Not secret -- static SDK telemetry, identical for every user.
AUTH0_SPA_CLIENT = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMTAuMCJ9"

# Name of the Auth0 SSO session cookie on AUTH_DOMAIN. This is the single
# value imported from a browser after signing in; see docs/session-cookie.md.
SESSION_COOKIE_NAME = "auth0"

CONF_SESSION_COOKIE = "session_cookie"
CONF_AGREEMENT_ID = "agreement_id"
CONF_TIMEZONE = "timezone"
CONF_TIME_UNIT = "time_unit"
# Poll interval in hours, editable from the integration's Configure
# dialog. Lives in entry.options rather than entry.data: options are the
# half a user owns, and keeping it out of data means the re-auth flow
# can rewrite credentials without touching it.
CONF_UPDATE_INTERVAL_HOURS = "update_interval_hours"
# Set once the historical backfill has run to completion. Until it is set
# the backfill restarts from the beginning, so an interrupted one (a
# restart mid-run) resumes rather than leaving a permanent hole.
CONF_BACKFILL_COMPLETE = "backfill_complete"

# Origin's UsageTimeUnit enum is BILLING, MONTHLY, DAILY, HOURLY,
# HALF_HOURLY. HOURLY is the sweet spot: HA's external statistics are hourly
# buckets, and Origin returns HOURLY points snapped to the local clock hour.
# That is also a UTC hour except in SA and the NT (UTC+9:30/+10:30), where
# the coordinator shares each point between the two UTC hours it straddles
# (see coordinator.hour_shares). HALF_HOURLY anchors its windows to the requested
# start time rather than the clock (a query starting 20:22 yields 20:22-20:51
# buckets), so it would need re-bucketing to gain nothing HA can store.
#
# Tried in order against the service's own availableUsageTimeUnits, since a
# basic accumulation meter won't offer anything below DAILY.
PREFERRED_TIME_UNITS = ("HOURLY", "DAILY")
DEFAULT_TIME_UNIT = "DAILY"

DEFAULT_TIMEZONE = "Australia/Sydney"

# Origin publishes usage once a day, in a single batch covering the whole
# previous local day -- but *when* that batch lands moves around. Measured
# on a live account (tools/probe_freshness.py, 92 polls over 30 h): one
# publication was pinned to between 08:53 and 09:13 local, while the next
# day's had still not arrived by 09:54 and was present by 18:26.
#
# A once-daily poll therefore races the batch, and losing that race parks
# the integration a full day behind until the following day's poll. The
# interval is well under that spread instead, so no single miss matters.
# Re-polling is cheap and safe: it is one GraphQL query, and the trailing
# re-import (see OVERLAP_DAYS) is idempotent.
DEFAULT_UPDATE_INTERVAL_HOURS = 4

# Bounds offered in the options flow. The lower bound is politeness
# towards a free cloud API rather than a technical limit; the upper is the
# old once-daily behaviour, which is still a reasonable choice for anyone
# who only looks at yesterday's totals.
MIN_UPDATE_INTERVAL_HOURS = 1
MAX_UPDATE_INTERVAL_HOURS = 24

# Re-fetch a small trailing window on every update since Origin sometimes
# revises the last day or two of usage/cost after the fact. This covers
# *revisions* only; how fresh the data is comes from the API's own
# lastReadingDate, and the overlap is measured back from that rather than
# from wall-clock now, so late-arriving data still gets covered.
OVERLAP_DAYS = 5

# Upper bound on the first-run backfill. The real start is the service's
# validFrom -- data simply does not exist before the agreement began, and
# asking for it returns windows of MISSING points -- so this only caps
# genuinely long-running accounts.
FIRST_RUN_HISTORY_DAYS = 730

# Retries are precautionary, not a workaround for a known fault: 40
# consecutive identical queries all succeeded, and every INTERNAL_SERVER_ERROR
# ever traced here turned out to have a deterministic cause. They exist
# because this is a cloud API polled once a day, where a single dropped
# connection would otherwise cost a whole day of data.
REQUEST_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2

# Origin caps a single usage query two different ways, and which cap binds
# depends on the granularity. Measured against a live account:
#
#   * DAILY and coarser hit the gateway's documented data-point ceiling.
#     Exactly 1000 daily points is accepted; 1200 is rejected up front with
#     "Range is too large. A range must generate less then 1000 data points."
#   * HOURLY and HALF_HOURLY hit an undocumented limit on elapsed days
#     first. 9 days succeeds at both (216 and 432 points respectively) and
#     10 days fails at both, so it counts days rather than points -- and the
#     backend reports it as an opaque "unexpected error" rather than as
#     validation, which makes it easy to mistake for an outage.
#
# The configured windows sit below the measured limits so an inclusive end
# boundary or a DST-lengthened day can't tip a query over.
MAX_QUERY_DAYS = {"HALF_HOURLY": 7, "HOURLY": 7, "DAILY": 900, "MONTHLY": 900}
DEFAULT_QUERY_DAYS = 7
