"""
Phase 2 incremental feature pull for the SBP forecaster.

Adds two new public data sources on top of the baseline `pull_incremental.py`:

  1. BMRS interconnector outturn — per-SP MW flow per interconnector
     /bmrs/api/v1/generation/outturn/interconnectors
     Aggregated to a single `ic_net_flow_mw` per SP (positive = net import).
     System tightness indicator: net imports cap upside, net exports tighten
     domestic generation.

  2. BMRS system-prices extended fields — already pulled for SBP/SSP/NIV
     but the response also carries BSAD-derived signals that we haven't been
     persisting: sellPriceAdjustment, buyPriceAdjustment, reserveScarcityPrice,
     priceDerivationCode, bsadDefaulted, totalAcceptedOfferVolume,
     totalAcceptedBidVolume. These are settlement-day indicators directly
     correlated with SBP outturn.

Path-2 / lag-96 invariant: features that depend on settled prices use lag-96
(2 days) in the Model B feature set. The pull itself is just adding columns
to the raw CSV layer — leakage handling happens at feature-build time.

Output:
    pipeline/data_raw/bmrs_interconnectors.csv  (per-SP per-IC outturn)
    pipeline/data_raw/bmrs_system_prices_v2.csv (extended SBP fields)

Idempotent: re-running on the same window dedupes by (settlementDate, SP, ic_name).

Usage:
    python pipeline/pull_phase2_features.py [--days 14]
"""

from __future__ import annotations

import argparse, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8")

BMRS = "https://data.elexon.co.uk/bmrs/api/v1"

HERE = Path(__file__).resolve().parent
import os as _os
RAW = Path(_os.environ.get("PIPELINE_RAW_DIR") or (HERE / "data_raw"))
RAW.mkdir(parents=True, exist_ok=True)

SLEEP_S = 0.25
RETRIES = 4
TIMEOUT = 60

t0 = time.time()
def log(m): print(f"[{(time.time()-t0):6.1f}s] {m}", flush=True)


def http_get(url, params=None):
    hdrs = {"Accept": "application/json"}
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, params=params, headers=hdrs, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as ex:
            if attempt < RETRIES - 1:
                time.sleep(2 ** attempt)
            else:
                log(f"  HTTP FAIL {url[:80]}: {ex}")
                return None


def merge_csv(path: Path, new_df: pd.DataFrame, dedup_cols: list[str]):
    if new_df.empty:
        log(f"  (no new rows for {path.name})")
        return
    if path.exists():
        existing = pd.read_csv(path)
        merged = pd.concat([existing, new_df], ignore_index=True)
        dedup_cols = [c for c in dedup_cols if c in merged.columns]
        if dedup_cols:
            merged = merged.drop_duplicates(subset=dedup_cols, keep="last")
        added = len(merged) - len(existing)
        merged.to_csv(path, index=False)
        log(f"  + {added:,} new rows → {path.name} ({len(merged):,} total)")
    else:
        new_df.to_csv(path, index=False)
        log(f"  + {len(new_df):,} new rows → {path.name} (file created)")


# ----------------------------------------------------------------------------
# Interconnectors — per-SP per-IC outturn
# ----------------------------------------------------------------------------
def pull_interconnectors(start: date, end: date):
    """Pull interconnector outturn and aggregate to net flow per SP.

    Endpoint returns one row per (date, SP, interconnector) — typically
    10 ICs (Eleclink, IFA, IFA2, BritNed, NEMO, Viking, NSL, Moyle,
    East-West, Greenlink). We collapse to a single 'ic_net_flow_mw' per SP
    so it slots cleanly into master.parquet without a column explosion."""
    log(f"BMRS interconnectors {start} → {end}")
    rows = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=6), end)
        data = http_get(
            f"{BMRS}/generation/outturn/interconnectors",
            params={"settlementDateFrom": cur.isoformat(),
                    "settlementDateTo":   chunk_end.isoformat()})
        if data is not None:
            payload = data.get("data", data)
            rows.extend(payload)
        time.sleep(SLEEP_S)
        cur = chunk_end + timedelta(days=1)
    if not rows:
        log("  (no interconnector rows returned)")
        return
    df = pd.DataFrame(rows)
    # Persist the raw per-IC stream so future analyses can use IC-specific signals
    merge_csv(RAW / "bmrs_interconnectors.csv", df,
              dedup_cols=["settlementDate", "settlementPeriod", "interconnectorName"])

    # Aggregate to net flow per SP for direct master.parquet ingestion
    df["settlementDate"] = pd.to_datetime(df["settlementDate"])
    agg = (df.groupby(["settlementDate", "settlementPeriod"], as_index=False)
             .agg(ic_net_flow_mw=("generation", "sum"),
                  ic_count=("interconnectorName", "nunique")))
    merge_csv(RAW / "bmrs_ic_net_flow.csv", agg,
              dedup_cols=["settlementDate", "settlementPeriod"])
    log(f"  aggregated → ic_net_flow_mw (n={len(agg):,} SPs)")


# ----------------------------------------------------------------------------
# System prices — extended fields (BSAD adjustments + scarcity + volumes)
# ----------------------------------------------------------------------------
SYSTEM_PRICE_FIELDS = [
    "settlementDate", "settlementPeriod", "startTime",
    "systemSellPrice", "systemBuyPrice",
    "netImbalanceVolume",
    # BSAD-derived: these are the marginal cashout adjustments — SBP signal
    "sellPriceAdjustment", "buyPriceAdjustment",
    "bsadDefaulted", "priceDerivationCode",
    # Reserve scarcity premium (one of the spike drivers)
    "reserveScarcityPrice",
    # Total accepted volumes (system tightness)
    "totalAcceptedOfferVolume", "totalAcceptedBidVolume",
    "replacementPrice", "replacementPriceReferenceVolume",
]

def pull_system_prices_extended(start: date, end: date):
    """Pull system-prices for each day and persist the extended field set.

    Keeps backward compatibility — the headline columns are identical to the
    baseline pull, but we also persist BSAD/scarcity/volume signals."""
    log(f"BMRS system-prices (extended) {start} → {end}")
    rows = []
    cur = start
    while cur <= end:
        d = http_get(f"{BMRS}/balancing/settlement/system-prices/{cur.isoformat()}")
        if d is not None:
            payload = d.get("data", d) if isinstance(d, dict) else d
            if payload:
                for row in payload:
                    # Strip to the field list to avoid column explosion
                    rows.append({k: row.get(k) for k in SYSTEM_PRICE_FIELDS})
        time.sleep(SLEEP_S)
        cur = cur + timedelta(days=1)
    df = pd.DataFrame(rows)
    if df.empty:
        log("  (no system-price rows returned)")
        return
    merge_csv(RAW / "bmrs_system_prices_v2.csv", df,
              dedup_cols=["settlementDate", "settlementPeriod"])


# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14,
                    help="Pull last N days (default 14)")
    args = ap.parse_args()

    today = date.today() + timedelta(days=1)
    start = today - timedelta(days=args.days)
    end   = today
    log(f"Phase 2 feature pull window: {start} → {end}  ({args.days} days)")

    pull_interconnectors(start, end)
    pull_system_prices_extended(start, end)

    log("Done.")


if __name__ == "__main__":
    main()
