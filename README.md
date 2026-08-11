# RevOpt — GB BESS Revenue Optimisation Pipeline

Short-term electricity price forecasting and battery revenue optimisation for
GB storage assets. Ingests public GB market data (BMRS, NESO, National Grid
Carbon Intensity) plus Spectron NBP gas, trains four walk-forward forecasting
models (day-ahead price, imbalance price, imbalance spike probability,
per-product ancillary clearing price), and solves a rolling multi-market LP
for 50 MW / 100 MWh reference assets. Includes a matched-objective
perfect-foresight oracle for capture-percentage benchmarking.

Delivered to Utilidex as the reproducibility layer for the MSc dissertation
"Short-Term Electricity Price Forecasting and Battery Revenue Optimisation:
A Multi-Market Framework for GB Storage Assets" (UCL MSc Business Analytics,
2025–26).

**Start here:** [HANDOVER.md](HANDOVER.md) — bootstrap, env vars, and the
end-to-end run recipe.

## Repository layout

```
.
├── HANDOVER.md              — Utilidex-facing setup + operations guide
├── README.md                — this file
├── SUPABASE_SETUP.md        — Supabase bucket / table schema
├── requirements.txt         — minimal runtime deps (for daily jobs)
├── requirements_full.txt    — full deps for training + optimisation
│
├── pipeline/                — everything Python
│   ├── 01_ingestion/        — data pulls + feature engineering
│   │   ├── pull_incremental.py       — BMRS/NESO/CI daily top-up
│   │   ├── pull_phase2_features.py   — IC net flow, BSAD adjustments
│   │   └── aggregate_boalf.py        — BOALF (Balancing Mech accepted offers)
│   ├── 02_master/           — master parquet assembly
│   │   └── build_master.py
│   ├── 03_models/           — four forecasting models
│   │   ├── retrain_model_a_lear.py   — LEAR (sparse linear day-ahead)
│   │   ├── retrain_model_a_lgbm.py   — LightGBM day-ahead
│   │   ├── ensemble_model_a.py       — LEAR + LGBM dynamic ensemble
│   │   ├── retrain_model_b.py        — SBP quantile XGBoost
│   │   ├── retrain_model_c.py        — spike-probability focal-loss XGBoost
│   │   └── retrain_model_d.py        — per-product ancillary quantile XGBoost
│   ├── 04_optimiser/        — LP + PF oracle
│   │   ├── lp_v6_ensemble.py         — LP v6 with Refinements R1–R6
│   │   └── compute_pf_v6.py          — matched-objective PF oracle
│   ├── 05_evaluation/       — output exporters
│   │   ├── export_to_mvp_snapshot.py — writes data/snapshot.json
│   │   └── export_forecasts_7day.py  — writes data/forecasts_7day.json
│   └── orchestration/       — top-level runners + shared helpers
│       ├── run_full_pipeline.py      — weekly retrain entrypoint
│       ├── refresh_snapshot.py       — daily top-up entrypoint
│       ├── supabase_io.py            — Supabase read/write helpers
│       ├── bootstrap_supabase.py     — first-time Supabase setup
│       └── diagnose_supabase.py      — connection / bucket sanity check
│
├── mvp/                     — Next.js + FastAPI dashboard (reference impl)
│   ├── app/, components/, lib/, public/  — Next.js 13 App Router frontend
│   ├── backend/                          — FastAPI serving snapshot.json
│   ├── package.json, tsconfig.json, tailwind.config.ts, next.config.js …
│   └── DEPLOY.md                         — MVP-specific Vercel deploy notes
│
├── data/                    — reference outputs (checked in)
│   ├── snapshot.json                     — latest LP dispatch + capture %
│   └── forecasts_7day.json               — forward 7-day price forecasts
│
└── .github/workflows/       — CI/CD
    ├── weekly_full_refresh.yml    — Sun 02:00 UTC — full retrain (~45–70 min)
    ├── daily_refresh.yml          — 08:30 UTC — snapshot top-up
    ├── daily_wind_pull.yml        — daily NESO wind + demand top-up
    └── rebuild_snapshot.yml       — manual snapshot rebuild
```

At runtime the pipeline also creates `pipeline/data/{raw,processed,proprietary,models_cache}/` for artefacts pulled from or pushed to Supabase. Those are gitignored — the source of truth is the Supabase bucket.

## MVP dashboard — reference only

Everything under `mvp/` is a Next.js + FastAPI reference implementation built
during the dissertation to visualise pipeline output. Utilidex is not expected
to deploy this as-is — it's included so engineers can see how
`data/snapshot.json` is consumed. Vercel configuration has been stripped from
the workflows so nothing tries to deploy on Utilidex's behalf.

## Not included

- **Dissertation submission artefacts** — model cards, thesis figures, AI
  usage audit. Available on request if needed for reference.

## Technical stack

- **Python 3.13**, pandas, pyarrow
- **LightGBM**, XGBoost, LEAR (sparse linear day-ahead)
- **PuLP** with COIN-OR CBC (LP solver — no commercial licence required)
- **Supabase** for parquet storage + service-role writes from CI
- **GitHub Actions** for scheduled retraining
