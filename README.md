<img src="brands/icon.png" width="72" align="right" alt="">

# Origin Energy for Home Assistant

Pulls your electricity usage and cost from Origin Energy into Home
Assistant's **Energy dashboard**, with hourly detail and full history back
to the start of your agreement.

> **Unofficial and independent.** This project is not affiliated with,
> endorsed by, or sponsored by Origin Energy Limited, who provide no
> support for it. "Origin Energy" and the Origin logo are their
> trademarks, used here only to identify the service this integration
> connects to. See [TRADEMARKS.md](TRADEMARKS.md).

![Australia only](https://img.shields.io/badge/region-Australia-blue)
![Electricity](https://img.shields.io/badge/fuel-electricity-yellow)
![hacs](https://img.shields.io/badge/HACS-custom%20repository-orange)

## What you get

- **Hourly** consumption and cost as long-term statistics, ready to select
  in the Energy dashboard.
- A **historical backfill** on first run, going back to the day your
  agreement started (or two years, whichever is shorter).
- **Revisions applied.** Origin restates the last day or two once meter
  data settles; each poll re-reads a trailing window and corrects what
  changed instead of leaving the first guess in place.
- One `sensor.origin_energy_last_update` timestamp for automations that
  want to know the import ran.

Origin publishes usage once a day and always a day or so behind, in a
single batch covering the whole previous day. The hour that batch lands
moves around, so this polls **every 4 hours** by default rather than
racing it once a day — change it under **Configure** on the integration
(1–24 hours). It is not, and cannot be, a live power meter — for that you
want a CT clamp or a smart plug.

## Requirements

- An Origin Energy **electricity** account with usage data in My Account.
  Gas is not supported; see [Limitations](#limitations).
- Home Assistant **2025.11 or newer** (it uses `mean_type` and
  `unit_class` on statistics metadata).
- The recorder integration enabled, which it is by default.

## Install

### HACS

1. Install [HACS](https://hacs.xyz/docs/setup/download) if you haven't already.
2. [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=JKook-Plus&repository=ha-origin-energy&category=integration)
3. Press **Download**.
4. Restart Home Assistant.

That button adds this repository to HACS for you. If it doesn't work — the
My Home Assistant links need to be [set up
once](https://my.home-assistant.io/) per instance — do it by hand instead:
**HACS → ⋮ → Custom repositories**, add this repository's URL with category
**Integration**, then install **Origin Energy** and restart.

### Manual

Copy `custom_components/origin_energy` into your `config/custom_components/`
directory and restart Home Assistant.

## Set up

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=origin_energy)

Or by hand: **Settings → Devices & services → Add integration → Origin
Energy.**

It asks for one thing: a session cookie. Origin's login form is behind
Kasada bot protection that refuses any non-browser client, so there is no
username-and-password path — you sign in once in a real browser and hand
over the resulting session.

To get it:

1. Sign in at [originenergy.com.au](https://www.originenergy.com.au)
   **with DevTools closed** — the bot protection fails the login silently
   if the panel is open.
2. In the same tab, go to
   `https://id.originenergy.com.au/.well-known/jwks.json`. It is just
   Auth0's public keys; the point is to land on that domain so its cookies
   become visible.
3. DevTools (<kbd>F12</kbd>) → **Application** (or **Storage** in Firefox)
   → **Cookies** → `https://id.originenergy.com.au`.
4. Copy the full value of the **`auth0`** cookie — click the row and copy
   from the detail pane, since the column truncates it. It starts with
   `s%3A`. Don't grab `auth0_compat`, which sits next to it and looks
   nearly identical.
5. Paste it into Home Assistant.

[docs/session-cookie.md](docs/session-cookie.md) has a fallback method and
more detail on why it works this way.

**You should only need to do this once.** Auth0 pushes the cookie's ~100
day expiry forward every time the integration refreshes, and the new value
is saved back, so a daily poll keeps the session alive indefinitely. If it
ever does lapse, Home Assistant raises a re-authentication prompt and your
history is kept.

## Add it to the Energy dashboard

Nothing appears automatically — you have to point the dashboard at the
statistics once. The first import runs in the background and takes a
minute or two, so if the dropdowns are empty, give it a moment and
reload.

Go to **Settings → Dashboards → Energy → Grid consumption → Add
consumption**, and in the *Configure grid connection* dialog:

| Field | Set it to |
|---|---|
| **Energy imported from grid** | `Origin Energy consumption` |
| **Energy exported to grid** | *leave empty* — solar export isn't imported yet |
| **Cost tracking** | **Use an entity tracking the total costs** |
| **Entity with the total costs** | `Origin Energy cost` |
| **Type of power measurement** | **No power sensor** |

Then **Save**.

Two of those are easy to get wrong. *Use an entity with current price*
looks plausible but is for a live tariff — Origin gives costs already
calculated, so the total-cost option is the right one. And **No power
sensor** is correct rather than a limitation: power sensors measure
instantaneous watts, and this data is hourly totals arriving a day late.
There is nothing live to attach.

Once saved, the dashboard shows your full history straight away rather
than starting from today, because the statistics for those months already
exist.

Statistics are named `origin_energy:<agreement>_energy_consumption` and
`origin_energy:<agreement>_energy_cost`.

### If it isn't in the dropdown

The list only offers statistics that already exist, so an empty dropdown
means the backfill hasn't written anything yet. Check
**Developer tools → Statistics** and filter for `origin_energy` — if
nothing is there, see [Troubleshooting](#troubleshooting).

## Limitations

Measured against a live account, not assumed —
[docs/api-reference.md](docs/api-reference.md) has the evidence.

- **Electricity only.** The API exposes gas usage in the same shape, but
  it has never been tested against a real gas account, so it is not
  offered rather than shipped on a guess.
- **A day behind.** Origin's own `lastReadingDate` is typically yesterday.
  Nothing can make this real-time.
- **History starts at your agreement.** Data does not exist before
  `validFrom`; earlier windows come back as empty buckets, not an error.
- **Solar export is not imported.** The API carries feed-in fields and the
  client reads them, but nothing has been verified against an account that
  actually has solar, so they are not written as statistics.
- **One meter per entry.** Accounts with several agreements get the active
  one; add a second entry for another.

## Troubleshooting

**"That session cookie was rejected."** The value is usually truncated —
copy it from the detail pane rather than the Value column. Check it starts
with `s%3A`, and that you took `auth0` rather than `auth0_compat`.

**"Origin's bot protection refused the request."** Different problem: the
request never reached the login server, so your cookie is probably fine.
Wait a few minutes and retry.

**Energy dashboard shows nothing.** The backfill runs in the background
after setup; give it a minute, then check
**Developer tools → Statistics** for `origin_energy:`. The dashboard only
lists statistics that already exist.

**Numbers stop updating.** Check the `sensor.origin_energy_last_update`
timestamp. Enable debug logging to see what the poll is doing:

```yaml
logger:
  logs:
    custom_components.origin_energy: debug
```

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
```

The suite runs offline against a faked session: agreement selection,
retries, request shape, running sums, units, de-duplication and the
daylight-saving fold. CI runs it on every push and pull request, and daily
against the latest Home Assistant.

`tools/verify_against_csv.py` reconciles the API against a CSV export
downloaded from My Account — the end-to-end check worth running after any
change to the query or import path. See [tools/README.md](tools/README.md).

## How it works

Home Assistant's long-term statistics take hourly buckets carrying an
absolute running sum, which is exactly the shape Origin returns: dated
totals rather than a ticking meter. So the integration writes **external
statistics** directly instead of faking an ever-increasing sensor — the
same approach core's `opower` integration uses for utility billing data.

**The energy data is not an entity.** `origin_energy:…_energy_consumption`
and `…_energy_cost` are statistics, not sensors: they live in the
recorder's `statistics` table, appear under **Developer tools →
Statistics**, and will not show up in **Developer tools → States** or in
templates. The Energy dashboard reads statistics directly, which is why
this works without an entity in between.

The one real entity is `sensor.origin_energy_last_update` — a timestamp of
the last successful import, for automations and troubleshooting. No energy
data flows through it.

Consequences worth knowing:

- Sums must be written in chronological order, so the backfill runs
  oldest-first and re-derives totals from the point before any window it
  re-imports.
- Re-importing is safe. Statistics are keyed by start time, so a
  re-run overwrites rather than duplicating.
- An interrupted backfill starts over rather than leaving a hole.

## Licence

The **code** is MIT — see [LICENSE](LICENSE).

That licence does not extend to Origin Energy's trademarks or logo, which
remain their property. If you fork or redistribute this, the marks are
yours to consider separately — [TRADEMARKS.md](TRADEMARKS.md) sets out the
position, including how to get them removed.
