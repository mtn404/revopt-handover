# Live pipeline (MVP refresh job)

This folder runs **inside the GitHub Action** on the MVP repo to refresh the
dashboard every morning. It is intentionally **lean** — it does NOT do
full historical training. The dissertation backtest lives in the sibling
`clean-pipeline/` folder and is frozen at `clean-pipeline/frozen/dissertation_v1/`.

## Persistent state: Supabase Storage

Pipeline state (parquets, models, proprietary inputs) lives in a **private
Supabase Storage bucket** rather than in git, because the parquets are too
large (~17 MB master + per-model predictions) to commit and the cron job
needs to pick up where the previous run left off.

**One-time setup**: see [`SUPABASE_SETUP.md`](./SUPABASE_SETUP.md).

Bucket layout:

```
bess-mvp/                      ← private bucket, service-role-key access only
  parquets/                    ← downloaded at start of run, uploaded at end
    master.parquet
    lp_v6_ensemble_dispatch.parquet
    lp_v6_ensemble_revenue.parquet
    pf_v6_revenue.parquet
    model_{a,b,c,d}_predictions.parquet
  models_cache/                ← fitted models, refreshed monthly by retrain job
    model_a_lear.pkl
    model_a_lgbm_extras.txt
    model_b.json, model_c.json, model_d.json
    last_retrained.txt
  proprietary/                 ← drop-zone for manual Spectron/UKA uploads
    Spectron_latest.xlsx
    uka_daily_interpolated.csv
    DA_Prices.xlsx
  snapshots/
    latest.json                ← always the most recent snapshot
    snapshot_YYYY-MM-DD.json   ← archival copies (one per day)
```

## Two workflows

The live pipeline runs on **two cadences**:

### Daily (`daily_refresh.yml`, 08:30 UTC)
Lightweight: pulls last 14 days of public market data, syncs to Supabase,
stamps the snapshot's `last_refreshed_utc`. Doesn't retrain or solve.

### Weekly (`weekly_full_refresh.yml`, Sunday 02:00 UTC)
The full pipeline:

```
Supabase pull  →  pull_incremental.py  →  vendored clean-pipeline  →  Supabase push  →  commit snapshot
 (parquets +       (last 14 days of         (build master → 5 models      (everything +     (data/snapshot.json
  raw CSVs +        BMRS/NESO/CI)            → ensemble → LP → PF          snapshot)         → git → Vercel)
  proprietary)                               → export)
```

Runs in ~45-70 min. Sets `pipeline_mode = "live"` on the snapshot.
The vendored clean-pipeline scripts live in `pipeline/clean_pipeline/` and
respect the `LIVE_TEST_END` env var to set their TEST_END dynamically.

### Why two cadences?

Walk-forward retrain across 5 years × 5 models is expensive. Running it daily
would burn through GH Actions minute quotas and add no value (models retrain
on month boundaries, predictions don't change day-to-day within a month).
Weekly is the right cadence: the dashboard updates with fresh real-data
dispatch every Sunday, and the daily timestamp shows the system is alive.

## Folder layout

| Path | What's in it | Persisted in… |
|---|---|---|
| `pipeline/pull_incremental.py` | Pulls last N days from BMRS, NESO, CI | git |
| `pipeline/supabase_io.py` | Storage helpers (download/upload/list) | git |
| `pipeline/bootstrap_supabase.py` | One-time uploader for the initial bucket state | git |
| `pipeline/refresh_snapshot.py` | Orchestrator: Supabase pull → fetch → solve → push | git |
| `pipeline/rebuild_master.py` *(TODO)* | Builds master.parquet from raw + proprietary | git |
| `pipeline/daily_solve.py` *(TODO)* | Loads cached models, solves LP for the latest day | git |
| `pipeline/data_raw/` | BMRS/NESO/CI CSVs (rebuilt every run, ~80 MB) | rebuilt per run |
| `pipeline/data_proprietary/` | Spectron NBP, UKA carbon, DA prices xlsx | **Supabase** (synced down) |
| `pipeline/data_processed/` | master.parquet + per-model prediction parquets | **Supabase** (synced down + up) |
| `pipeline/models_cache/` | Pickled fitted models for live inference | **Supabase** (synced down + up) |

## Bootstrapping (first run)

The historical parquets are **not committed** to the MVP repo (too large). On
the first run the pipeline pulls a wide enough window (default 60 days back)
and rebuilds master.parquet from scratch. Subsequent runs only pull the last
14 days and patch the rolling tail of master.parquet.

If the cached models are missing, the daily orchestrator skips inference,
exports a snapshot with `pipeline_mode = "backtest"`, and logs an explicit
warning. The dashboard then keeps showing the frozen backtest.

## Proprietary inputs

Spectron NBP gas (`Spectron_*.xlsx`) and UKA carbon (`uka_daily_interpolated.csv`)
are **manual uploads** — drop the refreshed files into `pipeline/data_proprietary/`
and commit. The live pipeline forward-fills the last known value if no new
data is available.

## Cron schedule

The GitHub Action runs at **08:30 UTC** daily — 15 min before the NESO EAC
gate closure (09:00 UTC), so the dashboard shows results for "what would
have been bid" by 09:00 each morning.

The workflow file is `.github/workflows/daily_refresh.yml`. To pause the
job, edit the cron schedule there.

## Status

- [x] `pull_incremental.py` — fetches BMRS + NESO + CI for last N days
- [x] `clean_pipeline/` — vendored copies of build_master, retrain_*, LP, PF, exporter
- [x] `LIVE_TEST_END` env var override on every vendored script
- [x] `run_full_pipeline.py` — orchestrator (weekly)
- [x] `refresh_snapshot.py` — orchestrator (daily, lightweight)
- [x] `daily_refresh.yml` cron at 08:30 UTC
- [x] `weekly_full_refresh.yml` cron at Sun 02:00 UTC

Once the user re-runs `bootstrap_supabase.py` to upload raw CSVs and the
first weekly run completes, the dashboard's Topbar pill flips from
`BACKTEST` to `LIVE` automatically.
