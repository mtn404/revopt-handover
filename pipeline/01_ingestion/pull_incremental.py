"""
Incremental data pull for the live MVP refresh.

Pulls the last N days (default 14) from the four public sources and
merges into existing CSVs in `pipeline/data_raw/`. Idempotent: re-running
on the same window will dedupe and converge.

Sources pulled:
  * BMRS day-ahead MID prices       /bmrs/api/v1/datasets/MID
  * BMRS system prices (SBP/SSP/NIV) /bmrs/api/v1/balancing/settlement/system-prices/{date}
  * BMRS demand outturn              /bmrs/api/v1/generation/outturn
  * NESO EAC ancillary clearings     /api/3/action/datastore_search_sql
  * NESO DA wind forecast            /api/3/action/datastore_search_sql
  * Carbon Intensity gen mix         /generation/{from}/{to}

Sources INTENTIONALLY NOT pulled here:
  * BMRS BOALF — 2.4 GB historical; not used in the live LP
  * NESO 2D/7D demand forecasts — used only in walk-forward training, not for
    daily inference (we use the DA forecast for the next day)
  * Yahoo Brent, FRED gas — proprietary NBP gas covers this in the live LP

Usage:
    python pipeline/pull_incremental.py [--days 14]
"""

import argparse, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8")

BMRS = "https://data.elexon.co.uk/bmrs/api/v1"
NESO = "https://api.neso.energy/api/3/action"
CI   = "https://api.carbonintensity.org.uk"

HERE = Path(__file__).resolve().parent
# Allow run_full_pipeline.py to redirect output to clean_pipeline/data/raw/
import os as _os
RAW = Path(_os.environ.get("PIPELINE_RAW_DIR") or (HERE / "data_raw"))
RAW.mkdir(parents=True, exist_ok=True)

SLEEP_S = 0.25
RETRIES = 4
TIMEOUT = 60

t0 = time.time()
def log(m): print(f"[{(time.time()-t0):6.1f}s] {m}", flush=True)


def http_get(url, params=None, headers=None):
    hdrs = {"Accept": "application/json", **(headers or {})}
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
    """Append new rows, dedupe, write back."""
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


# ------------------------------------------------------------------
# BMRS day-ahead MID prices.
#
# Elexon's /datasets/MID accepts up to ~7-day ranges. It also interprets
# `to=YYYY-MM-DD` as UTC midnight, which truncates UK-local day B SPs after
# 23:00 UTC. We compensate by chunking into 7-day windows and padding the
# end of each chunk by +1 day to capture BST/UTC boundary SPs. Dedup on
# (startTime, dataProvider) stitches everything together.
# ------------------------------------------------------------------
def pull_bmrs_da(start: date, end: date):
    log(f"BMRS DA MID  {start} → {end}  (7-day chunks, +1 day pad each)")
    rows = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=6), end)
        data = http_get(
            f"{BMRS}/datasets/MID",
            params={"from": cur.isoformat(),
                    "to":   (chunk_end + timedelta(days=1)).isoformat()})
        if data is not None:
            rows.extend(data.get("data", data))
        time.sleep(SLEEP_S)
        cur = chunk_end + timedelta(days=1)
    df = pd.DataFrame(rows)
    merge_csv(RAW / "bmrs_da_prices.csv", df,
              dedup_cols=["startTime", "dataProvider"])


# ------------------------------------------------------------------
# BMRS system prices (SBP)
# ------------------------------------------------------------------
def pull_bmrs_sbp(start: date, end: date):
    log(f"BMRS system prices  {start} → {end} (daily endpoint)")
    rows = []
    d = start
    while d <= end:
        data = http_get(f"{BMRS}/balancing/settlement/system-prices/{d.isoformat()}")
        if data is not None:
            rows.extend(data.get("data", data))
        time.sleep(SLEEP_S)
        d += timedelta(days=1)
    df = pd.DataFrame(rows)
    merge_csv(RAW / "bmrs_system_prices.csv", df,
              dedup_cols=["startTime", "settlementPeriod"])


# ------------------------------------------------------------------
# BMRS BOALF (Bid-Offer Acceptance Level Flagged) — chunked by day
# Pulls to a temporary CSV that aggregate_boalf.py then summarises.
# ------------------------------------------------------------------
def pull_bmrs_boalf(start: date, end: date, out_name: str = "bmrs_boalf_incremental.csv"):
    """Pull BOALF rows for a date window. Note: BMRS BOALF endpoint accepts
    1-day chunks, returns flat row-per-action format.

    Output is a temporary CSV (not the canonical raw bmrs_boalf.csv) so
    aggregate_boalf.py can read it in --mode incremental and then it gets
    discarded after the merge."""
    log(f"BMRS BOALF  {start} → {end} (1-day chunks)")
    rows = []
    d = start
    while d <= end:
        data = http_get(f"{BMRS}/balancing/acceptances/all/{d.isoformat()}")
        if data is not None:
            rows.extend(data.get("data", data))
        time.sleep(SLEEP_S)
        d += timedelta(days=1)
    if not rows:
        log(f"  (no BOALF rows for window)")
        return None
    df = pd.DataFrame(rows)
    out = RAW / out_name
    df.to_csv(out, index=False)
    log(f"  + {len(df):,} rows → {out.name}")
    return out


# ------------------------------------------------------------------
# BMRS demand outturn
# ------------------------------------------------------------------
def pull_bmrs_demand(start: date, end: date):
    """Same 7-day chunking + BST pad as DA prices."""
    log(f"BMRS demand outturn  {start} → {end}  (7-day chunks, +1 day pad each)")
    rows = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=6), end)
        data = http_get(
            f"{BMRS}/generation/outturn",
            params={"from": cur.isoformat(),
                    "to":   (chunk_end + timedelta(days=1)).isoformat()})
        if data is not None:
            rows.extend(data.get("data", data))
        time.sleep(SLEEP_S)
        cur = chunk_end + timedelta(days=1)
    df = pd.DataFrame(rows)
    merge_csv(RAW / "bmrs_demand_outturn.csv", df, dedup_cols=["startTime"])


# ------------------------------------------------------------------
# NESO EAC ancillary clearings
# Resource UUIDs sourced from clean-pipeline/01_data_pulls/pull_all.py
# ------------------------------------------------------------------
NESO_ANC_UUIDS = {
    "current_period": "596f29ac-0387-4ba4-a6d3-95c243140707",
    "daily":          "3c51a666-1c33-450e-a6eb-c9b4a0c91584",
}
NESO_PRODUCTS_SQL = "'DCH','DCL','DMH','DML','DRH','DRL'"

def pull_neso_ancillary(start: date, end: date):
    import urllib.parse
    log(f"NESO EAC ancillary clearings  {start} → {end}")
    all_rows = []
    for label, uuid in NESO_ANC_UUIDS.items():
        sql = (f'SELECT "auctionProduct" AS service, "deliveryStart", "deliveryEnd", '
               f'"clearingPrice" FROM "{uuid}" '
               f'WHERE "auctionProduct" IN ({NESO_PRODUCTS_SQL}) '
               f'AND "deliveryStart" >= \'{start.isoformat()}\' '
               f'AND "deliveryStart" <  \'{(end + timedelta(days=1)).isoformat()}\' '
               f'ORDER BY "deliveryStart"')
        url = f"{NESO}/datastore_search_sql?sql=" + urllib.parse.quote(sql)
        data = http_get(url, headers={"User-Agent": "Mozilla/5.0"})
        if data is None: continue
        records = data.get("result", {}).get("records", [])
        all_rows.extend(records)
        log(f"    {label}: +{len(records)} rows")
    df = pd.DataFrame(all_rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["service", "deliveryStart"])
    merge_csv(RAW / "neso_ancillary_clearing.csv", df,
              dedup_cols=["service", "deliveryStart"])


# ------------------------------------------------------------------
# NESO day-ahead forecasts
# ------------------------------------------------------------------
# Wind requires DUAL sources:
#   - historic: complete backfill of every day's day-ahead wind forecast once
#     issued and archived. Present for all past delivery dates.
#   - live:     tomorrow's day-ahead forecast, refreshed each morning ~08:40 UTC.
#     Not yet in the historic archive at the moment we pull.
# Merging both keeps Model A's wnd_today feature fully populated across the
# complete walk-forward window, avoiding the coverage holes that would
# otherwise trigger Model A to skip settlement periods.
NESO_WIND_RESOURCES = {
    "historic":  "7524ec65-f782-4258-aaf8-5b926c17b966",
    "live":      "b2f03146-f05d-4824-a663-3a4f36090c71",
}
# Demand is a single 2-day-ahead source (no dual-endpoint issue).
NESO_DEMAND_RESOURCE = "9847e7bb-986e-49be-8138-717b25933fbb"


def _fetch_neso_resource(rid: str, start: date, end: date):
    """Page through datastore_search for one resource. Bails after walking
    past `start` (assumes sort by _id desc gives ~date desc for these tables)."""
    rows, offset, limit = [], 0, 5000
    while True:
        data = http_get(f"{NESO}/datastore_search",
                        params={"resource_id": rid, "limit": limit,
                                "offset": offset, "sort": "_id desc"})
        if data is None: break
        recs = data.get("result", {}).get("records", [])
        if not recs: break
        rows.extend(recs)
        tail = recs[-1]
        tail_date = (str(tail.get("Datetime_GMT") or tail.get("date")
                         or tail.get("TARGETDATE") or tail.get("targetDate") or ""))[:10]
        if tail_date and tail_date < start.isoformat():
            break
        offset += limit
        if len(recs) < limit: break
    return rows


def pull_neso_forecasts(start: date, end: date):
    """Wind: dual-source (historic archive + live day-ahead) merged into one
    CSV. Demand: single-source.

    Backfill escape hatch: if the env var NESO_BACKFILL_START is set to an
    ISO date (YYYY-MM-DD), that date overrides `start` for THIS call only.
    Used to reconstruct the historic archive after the wind+demand CSVs
    in Supabase state collapsed to a rolling 30-day window."""

    _bf = _os.environ.get("NESO_BACKFILL_START")
    if _bf:
        try:
            start = date.fromisoformat(_bf.strip())
            log(f"NESO backfill mode — overriding start to {start}")
        except ValueError:
            log(f"NESO_BACKFILL_START={_bf!r} not ISO date — ignoring")

    # ---- WIND (dual source) ----
    wind_rows = []
    for label, rid in NESO_WIND_RESOURCES.items():
        log(f"NESO wind    {label:<8}  {start} → {end}")
        recs = _fetch_neso_resource(rid, start, end)
        wind_rows.extend(recs)
        log(f"    +{len(recs):,} rows from {label}")
    df_wind = pd.DataFrame(wind_rows)
    date_col = "Datetime_GMT" if "Datetime_GMT" in df_wind.columns else \
               ("date" if "date" in df_wind.columns else None)
    if date_col and not df_wind.empty:
        df_wind[date_col] = df_wind[date_col].astype(str)
        df_wind = df_wind[(df_wind[date_col].str[:10] >= start.isoformat()) &
                          (df_wind[date_col].str[:10] <= end.isoformat())]
    dedup_cols = [c for c in ["Datetime_GMT", "Settlement_period"] if c in df_wind.columns]
    if dedup_cols and not df_wind.empty:
        # keep="last" means the live-endpoint record wins when both are present
        # for the same (delivery time, SP) — the live one is typically issued
        # more recently and marginally more accurate.
        df_wind = df_wind.drop_duplicates(subset=dedup_cols, keep="last")
    merge_csv(RAW / "neso_da_wind_forecast.csv", df_wind, dedup_cols=dedup_cols)

    # ---- DEMAND (single source) ----
    log(f"NESO demand  single    {start} → {end}")
    dem_recs = _fetch_neso_resource(NESO_DEMAND_RESOURCE, start, end)
    df_dem = pd.DataFrame(dem_recs)
    date_col = "TARGETDATE" if "TARGETDATE" in df_dem.columns else \
               ("targetDate" if "targetDate" in df_dem.columns else None)
    if date_col and not df_dem.empty:
        df_dem[date_col] = df_dem[date_col].astype(str)
        df_dem = df_dem[(df_dem[date_col].str[:10] >= start.isoformat()) &
                        (df_dem[date_col].str[:10] <= end.isoformat())]
    merge_csv(RAW / "neso_da_demand_forecast.csv", df_dem,
              dedup_cols=[c for c in [date_col, "CARDINALPOINT", "CP_ST_TIME"]
                          if c and c in df_dem.columns])


# ------------------------------------------------------------------
# Carbon Intensity generation mix
# ------------------------------------------------------------------
def pull_ci_gen_mix(start: date, end: date):
    """Pulls CI gen mix in LONG format (one row per timestamp × fuel),
    matching clean-pipeline's pull_all.py shape. build_master.py merges
    on (from, fuel) so dedup needs both columns."""
    log(f"Carbon Intensity gen mix  {start} → {end}")
    rows = []
    d = start
    while d <= end:
        fr = f"{d.isoformat()}T00:00Z"
        to = f"{d.isoformat()}T23:30Z"
        data = http_get(f"{CI}/generation/{fr}/{to}")
        if data is not None:
            for entry in data.get("data", []):
                # LONG format: one row per fuel for this timestamp
                for m in entry.get("generationmix", []):
                    rows.append({
                        "from": entry["from"],
                        "to":   entry["to"],
                        "fuel": m["fuel"],
                        "perc": m["perc"],
                    })
        time.sleep(SLEEP_S)
        d += timedelta(days=1)
    df = pd.DataFrame(rows)
    merge_csv(RAW / "ci_generation_mix.csv", df, dedup_cols=["from", "fuel"])


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14,
                    help="How many days back from yesterday to pull (default 14)")
    args = ap.parse_args()

    # Pull through TOMORROW (UTC). The forward LP solve recommends tomorrow's
    # dispatch, so we need:
    #   - BMRS DA prices for tomorrow (cleared today 11:00 UTC) ✓
    #   - NESO EAC ancillary clearings for tomorrow (cleared today 09:00 UTC) ✓
    #   - NESO DA forecasts for tomorrow (published today) ✓
    # SBP / NIV / demand outturn for tomorrow won't settle until the day after —
    # that's fine, the lag-96 models use data ≥ 2 days old anyway.
    today    = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)
    start    = today - timedelta(days=args.days - 1)

    log("=" * 70)
    log(f"INCREMENTAL PULL — window {start} → {tomorrow} ({args.days + 1} days)")
    log("=" * 70)

    pull_bmrs_da(start, tomorrow)
    pull_bmrs_sbp(start, tomorrow)
    pull_bmrs_demand(start, tomorrow)
    pull_neso_ancillary(start, tomorrow)
    pull_neso_forecasts(start, tomorrow)
    pull_ci_gen_mix(start, tomorrow)

    log("Done. Files in: " + str(RAW))


if __name__ == "__main__":
    main()
