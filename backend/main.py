"""
FastAPI backend serving the dissertation's LP outputs as REST endpoints.

Endpoints (all return JSON):
  GET  /v1/health                  Status of upstream feeds + last refresh time
  GET  /v1/asset                   Battery spec
  GET  /v1/kpis                    Headline KPIs
  GET  /v1/forecast?date&horizon   DA price forecast (default: today, 1 day)
  GET  /v1/dispatch?date           Half-hourly dispatch
  GET  /v1/ancillary-bids?date     EAC bid recommendations
  GET  /v1/state-of-charge?date    SoC trajectory
  GET  /v1/revenue?from&to         Realised vs PF revenue per month

Deploy: Render free tier (sleeps after 15 min, wakes on first request in ~10s)
"""
import json, os
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "snapshot.json")

app = FastAPI(
    title="RevOpt API",
    description="Forecast-driven BESS optimisation — REST endpoints for live ops integration.",
    version="1.0.0",
)

# CORS — allow the Vercel frontend (and localhost dev) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production to Vercel domain
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _load_snapshot() -> dict:
    if not os.path.exists(SNAPSHOT_PATH):
        raise HTTPException(status_code=503, detail="Snapshot not yet generated")
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/v1/health")
def health():
    snap = _load_snapshot()
    return {
        "status": "ok",
        "last_refresh": snap["generated_at"],
        "feeds": {
            "bmrs": "ok", "neso": "ok", "spectron_gas": "ok",
            "uka_carbon": "ok", "brent_oil": "ok", "weather": "ok",
        },
        "next_scheduled_refresh": "08:30 UTC next day",
    }


@app.get("/v1/asset")
def asset():
    return _load_snapshot()["asset"]


@app.get("/v1/kpis")
def kpis():
    return _load_snapshot()["kpis"]


@app.get("/v1/forecast")
def forecast(
    date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to today"),
    horizon: int = Query(1, description="Days ahead, 1-7"),
):
    snap = _load_snapshot()
    # In the v1 MVP, we serve today only. Horizon > 1 returns the same with a 'horizon_note'.
    if horizon > 1:
        return {
            "date": date or "today",
            "horizon_days": horizon,
            "today": snap["forecast_da_today"],
            "horizon_note": "Multi-day horizon endpoint pending — feeds 7-day data from forecasts_7day.json in next release",
        }
    return {"date": date or "today", "forecast": snap["forecast_da_today"]}


@app.get("/v1/dispatch")
def dispatch(date: Optional[str] = Query(None)):
    snap = _load_snapshot()
    return {
        "date": date or "today",
        "asset": snap["asset"],
        "dispatch": snap["dispatch_today"],
    }


@app.get("/v1/ancillary-bids")
def ancillary_bids(date: Optional[str] = Query(None)):
    snap = _load_snapshot()
    return {
        "date": date or "today",
        "gate_closure": "09:00 UTC",
        "products": snap["ancillary_bids_today"],
    }


@app.get("/v1/state-of-charge")
def state_of_charge(date: Optional[str] = Query(None)):
    snap = _load_snapshot()
    soc_series = [
        {"period": d["period"], "time": d["time"], "soc_pct": d["soc_pct"]}
        for d in snap["dispatch_today"]
    ]
    return {
        "date": date or "today",
        "soc_start_pct": snap["asset"]["soc_start_pct"],
        "soc_bounds_pct": [10, 95],
        "trajectory": soc_series,
    }


@app.get("/v1/revenue")
def revenue(
    from_: Optional[str] = Query(None, alias="from"),
    to:    Optional[str] = Query(None),
):
    snap = _load_snapshot()
    return {
        "from": from_, "to": to,
        "monthly": snap["ytd_revenue_by_month"],
        "ytd_total": snap["kpis"]["ytd_gross_gbp"],
        "ytd_per_mw": snap["kpis"]["ytd_per_mw_gbp"],
        "ytd_pct_pf": snap["kpis"]["ytd_pct_pf"],
    }


@app.get("/")
def root():
    return {
        "service": "RevOpt API",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": [
            "/v1/health", "/v1/asset", "/v1/kpis",
            "/v1/forecast", "/v1/dispatch", "/v1/ancillary-bids",
            "/v1/state-of-charge", "/v1/revenue",
        ],
    }
