# Spanish electricity demand forecasting

> **Status: work in progress.** The data pipeline (ingestion + historical
> backfill for both data sources) is done and running. Feature engineering,
> modeling, and the serving API are not built yet. See [Roadmap](#roadmap).

A day-ahead forecasting project for peninsular Spain's hourly electricity
demand: predict all 24 hours of day D+1 using only information available by
12:00 on day D (the Spanish day-ahead market's closing time), then compare
against the system operator's own published forecast.

## Why this exists

This is a portfolio project, but it's built the way I'd want a real forecasting
pipeline built: pick a target you can actually verify, don't trust a data
source just because it returns 200, and write down *why* a decision was made
so it doesn't get re-litigated blind six weeks later.

The first data source considered (REE's public API) looked fine on the
surface and turned out to be unauditable — it silently ignores filter
parameters and its gaps are invisible to a plain null check. That's documented
in detail, including how it was caught, in [DEVLOG.md](DEVLOG.md). The project
switched to ENTSO-E instead, which is what's implemented today.

## What's being predicted

- **Target:** actual electricity demand in Spain (MW), from ENTSO-E's
  `Actual Load` for area `ES`.
- **Horizon:** all 24 hours of day D+1, predicted at once, with a 12:00
  information cutoff on day D.
- **Baselines:** a seasonal-naive baseline (same hour, same weekday, last
  week) that the model has to beat, and the system operator's own day-ahead
  forecast as a benchmark it isn't expected to beat — the goal is getting
  close to it using only public data.

Full reasoning on the target, metric, validation scheme, and data source
evaluation is in [DEVLOG.md](DEVLOG.md).

## Data sources

| Source | Provides | Status |
|---|---|---|
| [ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) | Actual + forecasted electricity demand for Spain | Historical backfill implemented |
| [AEMET OpenData](https://opendata.aemet.es/) | Daily temperature (6 cities) + hourly forecast | Historical backfill + daily capture implemented |

Both require a free API key (`ENTSOE_API_KEY`, `AEMET_API_KEY`), requested
from each provider directly.

## Pipeline so far

```
                one-off backfill                daily cron (12:05)
                ┌─────────────────┐              ┌──────────────────┐
ENTSO-E  ──────►│ ingest_entsoe_   │              │                  │
                │ historico.py     │              │                  │
                └─────────────────┘              │                  │
                ┌─────────────────┐              │  ingest_aemet.py  │
AEMET    ──────►│ ingest_aemet_    │              │  (forecast       │
                │ historico.py     │              │   capture)       │
                └─────────────────┘              └──────────────────┘
                        │                                  │
                        ▼                                  ▼
              data/entsoe/*.csv                 data/aemet_predicciones/
              data/aemet_historico/*.csv         {date}.jsonl
```

`data/` and `.env` are gitignored — the scripts regenerate everything from
the two APIs above.

### Why there's a daily cron in a portfolio project

AEMET doesn't publish historical forecasts, only observed values — so the
model will train on *observed* temperature but, in production, only has
*forecast* temperature available at inference time. That's a leakage source
that can't be fixed retroactively. The daily cron persists AEMET's forecast
with a capture timestamp starting now, so this can be corrected properly once
enough days have accumulated. Documented in detail in DEVLOG.md under
"Temperature data leakage."

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync
cp .env.example .env   # fill in AEMET_API_KEY and ENTSOE_API_KEY
```

Run the backfills (one-off, safe to re-run to extend the range to today):

```bash
uv run ingest-entsoe-historico
uv run ingest-aemet-historico
```

Run the daily forecast capture (meant to run once a day, e.g. via a
scheduler — see DEVLOG.md for how it's currently scheduled):

```bash
uv run ingest-aemet
```

## Project structure

```
src/demanda_electrica_model/
├── ingest_entsoe_historico.py   # ENTSO-E backfill: actual + forecast demand
├── ingest_aemet_historico.py    # AEMET backfill: daily temperature
└── ingest_aemet.py              # AEMET daily forecast capture (cron)
notebooks/                       # exploration (not versioned)
data/                             # generated locally, gitignored
```

## Roadmap

- [x] Data source evaluation and target selection
- [x] Historical backfill (ENTSO-E demand, AEMET temperature)
- [x] Daily forecast persistence (to avoid a future leakage blind spot)
- [ ] EDA
- [ ] Feature engineering (calendar, lags, temperature)
- [ ] Model + walk-forward validation
- [ ] Serving API
- [ ] Deployment

Explicitly out of scope: MLflow, drift monitoring, scheduled retraining,
dashboard, CI/CD — a possible follow-up project, not this one.

## License

[MIT](LICENSE)
