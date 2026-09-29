# Tools

Scripts used to work out what Origin's API does and to check the
integration still agrees with it. Nothing here is needed to *run* the
integration.

Everything is **read-only**. The gateway exposes 13 mutations — payment
methods, refunds, move-out submissions, self meter reads — and none of
these scripts call any of them. Payment, bank and card fields are never
queried.

## Setting up

Most scripts need a live session. Copy the template and paste your cookie
into it (see [../docs/session-cookie.md](../docs/session-cookie.md)):

```bash
cp origin_secrets.env.example origin_secrets.env
```

They import the integration package, so the easiest place to run them is
wherever Home Assistant already is:

```bash
docker cp ../custom_components/origin_energy homeassistant:/tmp/origin_energy_test/
docker cp . homeassistant:/tmp/origin_energy_test/

set -a; . origin_secrets.env; set +a
docker exec -i -e ORIGIN_SESSION_COOKIE \
    homeassistant python3 /tmp/origin_energy_test/test_login.py
```

Passing `-e ORIGIN_SESSION_COOKIE` with no value forwards it from your
shell, so the cookie never appears on a command line or in shell history.

## Tests

The offline unit tests live in `tests/` and run with pytest; see the main
README. The scripts here need a live account or a running Home Assistant.

| script | needs network | what it covers |
|---|---|---|
| `test_login.py` | yes | the cookie → token exchange works end to end |
| `verify_against_csv.py` | yes | API totals vs. a CSV export from My Account |
| `verify_in_ha.py` | no | what actually landed in Home Assistant's statistics |

`verify_in_ha.py` runs after the integration is set up and reads the
recorder database read-only: point counts, clock-hour alignment, whether
the running sums are monotonic and agree with their own increments, and —
given a CSV path — whether the stored daily totals match Origin's export.
It never opens `.storage`, which holds the session cookie.

`verify_against_csv.py` is the one that matters after changing a query or
the import path — it reconciles unit conversion, day boundaries,
HOURLY→daily rollup and chunk-boundary de-duplication in one pass. Download
a usage CSV from My Account and pass its path.

## Investigation

These produced the measurements in
[../docs/api-reference.md](../docs/api-reference.md). Kept because the
conclusions are only trustworthy if the method is inspectable.

| script | question it answered |
|---|---|
| `explore_schema.py` | what does the schema actually expose? (introspection is on) |
| `explore_data.py` | which fields are really populated for an account? (output masked) |
| `probe_granularity.py` | how fine can usage data get? |
| `probe_range_limits.py` | how large a window will the gateway serve? |
| `probe_limits_fine.py` | does the sub-daily cap count days or points? |
| `probe_usage_depth.py` | how far back does history go, and in what units? |
| `probe_field_support.py` | which fields can the backend actually resolve? |
| `probe_reliability.py` | is `INTERNAL_SERVER_ERROR` about query size, or flakiness? |
| `probe_filter_arg.py` | why does an otherwise identical query fail? (`filter: null`) |
| `probe_freshness.py` | what time of day does new usage data actually land? |

`probe_freshness.py` is the long-running one: it watches
`lastReadingDate` for a day or more and records the wall-clock moment it
advances, which is what a sensible update schedule should be pinned to.
Give it at least 26 hours (`--hours 30` by default) so a daily
publication cannot fall in the gap between the first and last poll, and
re-read the log later with `--summarise-only`.

### Writing another one

The gateway answers an unparsable document with
`INTERNAL_SERVER_ERROR: "There was an unexpected error"` — the same thing
it says for a genuine backend problem, with no line number and no parse
error. A hand-written probe that drops a `}` therefore looks exactly like
Origin having an outage, and every conclusion drawn from it is wrong.

Two habits avoid this:

- Build queries by editing `ACCOUNT_USAGE_QUERY` rather than retyping one.
- Assert the braces balance before sending:

  ```python
  assert sum((c == "{") - (c == "}") for c in query) == 0
  ```

## Capture

`capture_traffic.py` (Playwright) and `mitm_capture.py` (mitmproxy) record
the web app's own traffic, which is how the endpoints were found in the
first place. `sanitize_har.py` strips tokens, cookies and personal details
from a capture before it is shared.

Treat raw captures as credentials — they contain live bearer tokens. The
`.gitignore` excludes `*.har` and `*.sanitized.json`; keep it that way.
