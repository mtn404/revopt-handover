# RevOpt — GB BESS Revenue Optimisation Pipeline

Short-term electricity price forecasting and battery revenue optimisation for GB
storage assets. Ingests public GB market data (BMRS, NESO, National Grid
Carbon Intensity) plus Spectron NBP gas, trains four walk-forward forecasting
models (day-ahead price, imbalance price, imbalance spike probability,
per-product ancillary clearing price), and solves a rolling multi-market LP for
50 MW / 100 MWh reference assets. Includes a matched-objective perfect-foresight
oracle for capture-percentage benchmarking.

Delivered to Utilidex as the reproducibility layer for the MSc dissertation
"Short-Term Electricity Price Forecasting and Battery Revenue Optimisation: A
Multi-Market Framework for GB Storage Assets" (UCL MSc Business Analytics,
2025–26).

**Start here:** [HANDOVER.md](HANDOVER.md) — bootstrap, env vars, and the
end-to-end run recipe.

## What's in this repo

```
.
├── HANDOVER.md              — Utilidex-facing setup + operations guide
├── pipeline/                — data ingestion, models, LP + PF optimiser
│   ├── run_full_pipeline.py — one-shot entrypoint: pull → build → train → solve
│   ├── clean_pipeline/
│   │   ├── 02_master/       — master parquet build from raw + proprietary
│   │   ├── 03_models/       — Models A / B / C / D walk-forward training
│   │   ├── 04_optimiser/    — LP v6 ensemble + PF v6 oracle
│   │   └── 05_eval/         — per-year capture, ablation, sensitivity
│   ├── pull_incremental.py  — daily BMRS / NESO / CI top-up
│   ├── pull_phase2_features.py  — enriched feature engineering
│   ├── supabase_io.py       — Supabase Storage read/write helpers
│   ├── refresh_snapshot.py  — build snapshot.json for dashboards
│   ├── requirements.txt     — minimal runtime deps
│   ├── requirements_full.txt — full training + optimiser deps
│   ├── README.md            — deeper pipeline docs
│   └── SUPABASE_SETUP.md    — Supabase bucket / table schema
├── data/                    — reference outputs (snapshot.json,
│                              forecasts_7day.json) from a recent run
└── .github/workflows/       — CI/CD
    ├── weekly_full_refresh.yml  — Sun 02:00 UTC full retrain
    ├── daily_refresh.yml        — daily snapshot top-up
    ├── daily_wind_pull.yml      — daily NESO wind top-up
    └── rebuild_snapshot.yml     — manual snapshot rebuild
```

## Not included

- **MVP dashboard** (Next.js frontend + FastAPI backend + Vercel deployment) —
  built as a dissertation-era demo, not production-quality. Utilidex has its own
  product surface.
- **Dissertation submission artefacts** — model cards, thesis figures, AI usage
  audit. Available on request if needed for reference.

## Technical stack

- **Python 3.13**, pandas, pyarrow
- **LightGBM**, XGBoost, LEAR (sparse linear day-ahead forecaster)
- **PuLP** with COIN-OR CBC (LP solver — no commercial licence required)
- **Supabase** for parquet storage + service-role writes from CI
- **GitHub Actions** for scheduled retraining
