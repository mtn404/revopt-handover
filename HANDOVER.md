# Handover Guide — Utilidex Engineering

This document is written for whoever picks up the pipeline on the Utilidex side.
It covers: bootstrap, required credentials, the end-to-end run recipe, and the
operational moving parts.

For the full architectural / methodology writeup, see the accompanying
dissertation.

---

## 1. Prerequisites

- **Python 3.13** (the workflows are pinned to 3.13; earlier versions untested)
- **Git** and **GitHub Actions** (repo must be under a GitHub account with
  Actions enabled for the workflows to run)
- **Supabase project** — used as remote storage for parquets. See
  `pipeline/SUPABASE_SETUP.md` for the required buckets + tables.
- **~2 GB free disk** on the machine that runs `run_full_pipeline.py` (raw +
  processed parquet cache).

No commercial licence required — the LP uses PuLP + COIN-OR CBC.

## 2. Required environment variables

Set these as **GitHub Actions secrets** (Settings → Secrets and variables →
Actions) and also in a local `.env` if running the pipeline outside CI.

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Service-role key (writes parquets from CI) |
| `SUPABASE_BUCKET` | Bucket name for parquet storage (default: `parquets`) |

The Spectron NBP gas feed is expected at
`pipeline/clean_pipeline/data/proprietary/spectron_nbp.parquet`. Utilidex
already holds the Spectron licence — drop your latest export at that path
before the first run.

## 3. Local bootstrap

```bash
# 1. Install deps (full set for training + optimisation)
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r pipeline/requirements_full.txt

# 2. Set env
export SUPABASE_URL="https://xxx.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="eyJ..."
export SUPABASE_BUCKET="parquets"

# 3. Bootstrap Supabase (one time — creates buckets + tables)
python pipeline/bootstrap_supabase.py

# 4. Full end-to-end run (~45–70 min on 4-core / 16 GB)
python pipeline/run_full_pipeline.py
```

Output: refreshed `data/snapshot.json`, `data/forecasts_7day.json`, plus all
model + LP + PF parquets pushed to Supabase.

## 4. CI/CD — what runs when

| Workflow | Schedule | Duration | Purpose |
|---|---|---|---|
| `weekly_full_refresh.yml` | Sun 02:00 UTC | 45–70 min | Full pipeline: rebuild master parquet, walk-forward retrain all 5 models, solve LP + PF for full backtest, commit fresh `snapshot.json` |
| `daily_refresh.yml` | Daily | ~5 min | Top-up snapshot with the latest day's dispatch |
| `daily_wind_pull.yml` | Daily | ~2 min | Pull latest NESO wind + demand |
| `rebuild_snapshot.yml` | Manual | ~5 min | Rebuild snapshot from current parquets (no retraining) |

All workflows also support `workflow_dispatch` for manual triggering from the
Actions tab.

**Common failure modes:**
- **BMRS not yet populated** — if the daily run fires before SP 48 has settled
  (~00:30 UTC), Model A can't produce a same-day dispatch. Schedule with a
  buffer if you need fresh-day output.
- **NESO API state collapse** — if wind/demand endpoints return an incomplete
  window, use the `backfill_neso_start` input on `workflow_dispatch` to force a
  wider pull (e.g. `2021-01-01` to rebuild the historic archive).

## 5. The four forecasting models

Details in `pipeline/clean_pipeline/03_models/` and the dissertation §4.4.

| Model | Task | Technique |
|---|---|---|
| **A** | Day-ahead price (point) | LEAR + LightGBM dynamic ensemble |
| **B** | Imbalance SBP (distributional, P05–P95) | Per-quantile XGBoost, pinball loss |
| **C** | Imbalance spike probability | XGBoost + focal loss + monthly threshold refit |
| **D** | 6 ancillary clearing prices × 6 EFA blocks | Per-product quantile XGBoost |

All models retrain walk-forward on a monthly cadence — retraining is
CI-triggered, not per-request.

## 6. The LP + PF optimiser

`pipeline/clean_pipeline/04_optimiser/`:

- `lp_v6_ensemble.py` — production LP with **Refinements R1–R6**:
  - R1: Model D quantile refactor
  - R2: empirical ancillary acceptance-rate multiplier
  - R3: Gaussian bid-price optimiser (σ = (P95−P05) / 3.29)
  - R4: degradation cost recalibration (£5 → £18/MWh)
  - R5: per-product utilisation-failure penalty
  - R6: symmetric £2/MWh execution slippage
- `compute_pf_v6.py` — **matched-objective perfect-foresight oracle** — shares
  the LP's two spike-conditioned safety rules with binary spike labels
  substituted for Model C's probabilities. Isolates forecast quality as the sole
  source of the capture gap.

Tunable via env vars: `LP_P_MAX` (default 50 MW), `LP_E_MAX` (default 100 MWh),
`LP_VOLUME_CAP` (default 0.70), `LP_OUTPUT_SUFFIX` (for parallel sensitivity
runs).

## 7. Data flow

```
BMRS      ┐
NESO      │
Carbon    ├─ pull_incremental.py ─┐
Intensity │                        │
Spectron  ┘                        ├─ build_master.py ─→ master.parquet
                                   │      (Supabase)
                                   │
                                   ├─ retrain_model_[a/b/c/d].py ─→ *_predictions.parquet
                                   │      (walk-forward, monthly)
                                   │
                                   ├─ lp_v6_ensemble.py ─→ lp_v6_ensemble_revenue.parquet
                                   ├─ compute_pf_v6.py  ─→ pf_v6_revenue.parquet
                                   │
                                   └─ refresh_snapshot.py ─→ data/snapshot.json
                                                              (checked into git)
```

## 8. Support / questions

Contact the original author for methodology questions during the initial
handover period. The dissertation §4 (Methodology) and §5 (Results) are the
authoritative reference for design decisions.
