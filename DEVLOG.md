# Development log

Condensed, English rewrite of the working dev journal kept during this project.
It records the decisions that shaped the data pipeline and why — including the
ones that didn't pan out. Dated entries refer to when the work happened.

## What's being predicted

Hourly (quarter-hourly since 2022-05-23, see ENTSO-E section below) peninsular
electricity demand in Spain.

- **Target:** actual load (MW), sourced from ENTSO-E (`Actual Load`, area `ES`).
- **Horizon:** all 24 hours of day D+1, predicted at once.
- **Information cutoff:** 12:00 on day D — matches OMIE's day-ahead market
  closing time. Everything fed to the model has to be available by then.
- **Planned features:** calendar (hour, weekday, month, public holiday,
  working day / bridge day), demand lags, temperature. Calendar and demand
  history carry most of the signal; temperature adds the (non-linear) weather
  effect — consumption rises with both cold and heat.

## Metric and baselines

- **Metric:** MAPE as the headline number, MAE (in MW) reported alongside.
- **Baseline 1 (seasonal naive):** same hour, same weekday, previous week.
  Has to be beaten.
- **Baseline 2 (system operator forecast):** ENTSO-E also publishes the
  operator's own day-ahead forecast (`Forecasted Load`). This is the
  professional benchmark — not expected to be beaten. The headline result is
  "how close to the operator's forecast using only public data."

## Validation

Walk-forward with a moving/expanding origin. At least 12 origins to get an
error *distribution*, not a single number. Never shuffled.

## Data sources

### REE — discarded (2026-08-17)

`apidatos.ree.es` (no token required), `demanda/demanda-tiempo-real` widget.
Looked usable at first — 200 responses, clean-looking data — but turned out
impossible to trust:

- **Silently ignores query parameters.** `time_trunc=hour` does nothing (always
  returns 5-min resolution, 288 points/day). `geo_limit` also does nothing:
  `peninsular`, `canarias`, and no filter all returned **identical** values —
  and Canarias reporting 23.5 GW is physically impossible. `geo_trunc=electric_system`
  returns 400. There is no way to request a geographic scope and confirm you
  got it.
- **Gaps are missing rows, not nulls.** January 2024: `Prevista` and
  `Programada` return 8640 values, `Real` returns 8425, and `isna()` reports
  zero nulls. 17 consecutive hours are simply absent starting 2024-01-07.
  Any null check is blind here, and a positional `shift()` stops being a
  temporal lag the moment there's a gap.
- **Schema changes across dates.** 3 series in 2022/2024, a 4th
  (`Programada total`) appears in recent dates.
- `demanda/evolucion`, which would have been the independent cross-check for
  scope and aggregation, returns 400.

**Why it was dropped:** no single issue is fatal on its own, but together
they mean the target can't be verified — geographic scope can't be confirmed,
the cross-check endpoint doesn't respond, and gaps only show up by comparing
row counts against a full time grid. Better to switch source now than to
discover this while debugging model errors later. The exploration behind this
call lives in `notebooks/ingest.ipynb` (kept locally, not versioned).

### ENTSO-E Transparency Platform — chosen, replaces REE (2026-08-27)

Accessed via `entsoe-py`'s `EntsoePandasClient` (not raw HTTP — the underlying
API is XML, and the library parses it into pandas and handles pagination and
retries). Requires `ENTSOE_API_KEY` in `.env`.

The lesson from discarding REE: before committing to a source, confirm it can
be *verified* — explicit scope, detectable gaps, a stable schema, and some
independent point of comparison. ENTSO-E clears that bar: scope is an
explicit per-country area code (not a silently-ignored parameter like REE's
`geo_limit`), and the operator's own forecast (`query_load_forecast`) is
exactly the independent cross-check REE didn't have.

Verified live against the API (`ES`, 2022-01-01 → 2026-08-27, `Actual Load`
and `Forecasted Load`):

- **Resolution changes over time, and that's correct, not a bug.** Hourly
  (24 points/day) up to 2022-05-22; quarter-hourly (96 points/day, `PT15M`)
  from 2022-05-23 onward — a real change in how Spain reports to ENTSO-E, not
  something to normalize during ingestion. Left as-is; handled at the
  feature-engineering stage.
- **Gaps are missing rows, same as REE** — must be checked against a full
  time grid, `isna()` won't see them. Unlike REE, they're few and explainable:
  one missing value on Jan 1st for 2023/2024/2025, one on 2023-11-21, and one
  large gap on **2025-04-28** (~46 of 96 values) matching the Iberian
  blackout that day — a real gap in the source, not an ingestion artifact.
  Zero duplicates, zero explicit nulls across four years.
- **No per-request range limit** (unlike AEMET's 6-month cap): a single
  2022→2026 request works in one call, the client paginates internally.
- **`Actual Load` publication lag: ~1 hour** (faster than REE's ~2h),
  confirmed by requesting "now" and comparing the latest timestamp returned
  against wall-clock time.

AEMET's historical temperature data still works fine as a feature source
regardless of which platform publishes the demand target.

### AEMET — opendata.aemet.es (requires an API key)

| Endpoint | Content | Use |
|---|---|---|
| `valores/climatologicos/diarios/.../estacion/{idema}` | Daily: tmax, tmin, tmed, precipitation, wind. Long history | Training |
| `observacion/convencional/datos/estacion/{idema}` | Hourly, last 12–24h only | Only useful if polled daily |
| `prediccion/especifica/municipio/horaria/{municipio}` | Hourly forecast, D..D+2 | Real inference + daily persistence |

Implementation notes:
- **Two-step pattern**: the first call returns JSON with a `datos` field
  that's a temporary URL; a second call to that URL returns the actual data.
- The daily endpoint caps each request at exactly 6 months (confirmed: 180
  days works, 365 fails with "date range cannot exceed 6 months") → requests
  are chunked into 6-month windows. Low enough volume (~10 requests per
  station for 4 years) that no resumable on-disk cache was needed — HTTP
  session retries are enough.

## Known limitations

### 1. AEMET has no hourly historical temperature

Historical temperature is **daily only** (tmax/tmin/tmed), not hourly.
Decision: stay at daily resolution and let the model recover intraday shape
from hour-of-day. Sufficient for this project, avoids adding dependencies.

Fallback if daily temperature proves too coarse: Open-Meteo (ERA5 reanalysis)
provides free, keyless hourly historical temperature. Not pursued until
there's evidence it's needed.

### 2. Temperature data leakage (the important one)

The model trains on **observed** temperature for day D+1, but in production,
at 12:00 on day D, that observed value doesn't exist yet — only AEMET's
**forecast** does. Training on observed and inferring on forecast is a leak:
the model will degrade in production in a way that hasn't been measured.

Training directly on AEMET's own past forecasts would be the correct fix, but
AEMET doesn't publish historical forecasts — they'd have to be accumulated
for months before there's enough to train on. Not viable as this project's
starting point.

**Decision:** train on observed temperature, document the leak, and
**start persisting daily forecasts today** so this can be done properly
later.

## Daily persistence

A daily cron job saves, with a **capture timestamp**, AEMET's hourly forecast
for the selected municipalities. Format: one `.jsonl` file per date, no
database, no abstractions — every day without this cron is unrecoverable
history.

Implemented in [`src/demanda_electrica_model/ingest_aemet.py`](src/demanda_electrica_model/ingest_aemet.py)
(`uv run ingest-aemet`). Captures the hourly forecast for 6 municipalities
(Madrid, Barcelona, Valencia, Sevilla, Bilbao, Zaragoza) and appends one line
to `data/aemet_predicciones/{date}.jsonl` with `captured_at` plus the full
per-municipality response. Scheduled via Windows Task Scheduler, daily at
12:05 (just after the 12:00 information cutoff).

Notable gotchas:
- The forecast endpoint ignores any date parameter — it's always "from now"
  (D..D+2). No backfill is possible, which is exactly why this couldn't wait.
- AEMET declares `charset=ISO-8859-15` in the response header but actually
  sends `iso-8859-1`; trusting the declared charset mangles accented names
  (e.g. `ValÚncia` instead of `València`).
- HTTP retries with backoff (4 attempts) so a brief network blip doesn't cost
  a whole day. If a municipality still fails, the script records
  `{"error": ...}` for it, continues with the rest, and exits with code 1 so
  the scheduler flags the run as failed instead of "green" with broken data
  inside.

## Historical backfills — implemented

- **AEMET** ([`ingest_aemet_historico.py`](src/demanda_electrica_model/ingest_aemet_historico.py),
  `uv run ingest-aemet-historico`): one-off backfill of daily climatology,
  2022-01-01 → today, for the 6 cities, chunked into 6-month windows. Output:
  `data/aemet_historico/temperaturas_diarias.csv`, one row per station-day,
  raw as returned (cleanup happens later, at feature-engineering time).

  Station selection (`idema`, a different master list from the forecast
  municipalities): prioritized the urban-core station over the airport one
  where an alternative existed, except where it had real gaps in the data —
  not a matter of preference:
  - Barcelona: `0201D` (Port Olímpic) has history back to 2015 but showed
    gaps in recent reporting (1 missing day out of 5 in a July 2026 check) →
    used `0076` (Airport) instead.
  - Madrid: `3195` (Retiro) had an **82-day unbroken streak** without `tmed`
    (2025-09-29 to 2025-12-19) that didn't show up in the overall null rate
    (~1.4%, looked normal) because it was all concentrated in one stretch —
    a reminder to check *max consecutive NaN streaks*, not just totals.
    `3129` (Airport) doesn't have that problem over the same period → used
    instead.

- **ENTSO-E** ([`ingest_entsoe_historico.py`](src/demanda_electrica_model/ingest_entsoe_historico.py),
  `uv run ingest-entsoe-historico`): one-off backfill of Spanish demand,
  2022-01-01 → today, via `query_load` (actual) and `query_load_forecast`
  (day-ahead operator forecast). Output: `data/entsoe/demanda_historica.csv`,
  a single wide CSV indexed by timestamp with `actual_load_mw` and
  `forecast_load_mw`, outer-joined so a gap in one series doesn't drop a row
  where the other has data.

Both backfills start in 2022 to leave the pandemic period out and keep the
same date range across sources.

## Scope

**In scope:** ingestion, EDA, features, model, a deployed API that serves
predictions, README.

**Out of scope (a follow-up project):** MLflow, drift monitoring, scheduled
retraining, dashboard, CI/CD.
