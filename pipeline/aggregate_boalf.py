"""
BOALF aggregator — converts raw BMRS Bid-Offer Acceptance Level Flagged
records into a half-hourly summary that build_master.py can ingest directly.

WHY: the raw bmrs_boalf.csv is ~2.4 GB (one row per individual BM action,
~50M rows across 5 years). The forecast models only use 6 aggregate columns
per half-hour, which collapses to ~17 MB. Storing the aggregate in Supabase
instead of the raw file keeps us inside the free tier without losing any
feature fidelity.

Two modes:

  * Bootstrap (--mode bootstrap):
        Read the full raw bmrs_boalf.csv and write boalf_aggregates.csv.
        Run once locally to seed the cloud copy. ~5 min for 2.4 GB.

  * Incremental (--mode incremental --since YYYY-MM-DD):
        Read a SUBSET of raw BOALF (just the last N days), aggregate it,
        and merge into the existing boalf_aggregates.csv (overwriting any
        rows in the overlap window). Used by run_full_pipeline.py weekly.

Output schema (boalf_aggregates.csv):
    bucket               UTC half-hour timestamp (e.g. 2026-06-01 14:30:00+00:00)
    boalf_count          total actions in that half-hour
    boalf_offer_count    # of offers accepted (levelTo > levelFrom)
    boalf_bid_count      # of bids accepted    (levelTo < levelFrom)
    boalf_total_mw       sum of |delta_mw|
    boalf_max_mw         max |delta_mw|
    boalf_so_count       # of system-operator-flagged actions
"""

from __future__ import annotations

import argparse, sys
from pathlib import Path
from typing import Iterator
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

CHUNK_ROWS    = 500_000
USECOLS       = ["timeFrom", "timeTo", "levelFrom", "levelTo", "soFlag"]
AGG_COLS      = ["boalf_count", "boalf_offer_count", "boalf_bid_count",
                 "boalf_total_mw", "boalf_max_mw", "boalf_so_count"]


def aggregate_chunk(chunk: pd.DataFrame) -> pd.DataFrame:
    """Aggregate one raw chunk into per-half-hour rows.
    Mirrors build_master.py Phase 6 exactly so the column semantics match."""
    chunk = chunk.copy()
    chunk["timeFrom"] = pd.to_datetime(chunk["timeFrom"], utc=True)
    chunk["delta_mw"] = (chunk["levelTo"] - chunk["levelFrom"]).abs()
    chunk["is_offer"] = (chunk["levelTo"] > chunk["levelFrom"]).astype(int)
    chunk["is_bid"]   = (chunk["levelTo"] < chunk["levelFrom"]).astype(int)
    chunk["bucket"]   = chunk["timeFrom"].dt.floor("30min")
    agg = chunk.groupby("bucket").agg(
        boalf_count       = ("delta_mw", "count"),
        boalf_offer_count = ("is_offer", "sum"),
        boalf_bid_count   = ("is_bid",   "sum"),
        boalf_total_mw    = ("delta_mw", "sum"),
        boalf_max_mw      = ("delta_mw", "max"),
        boalf_so_count    = ("soFlag",   "sum"),
    )
    return agg


def iter_chunks(path: Path, since: pd.Timestamp | None = None) -> Iterator[pd.DataFrame]:
    """Yield filtered chunks from a raw BOALF CSV. If `since` is set,
    keep only rows with timeFrom >= since."""
    for chunk in pd.read_csv(path, usecols=USECOLS, chunksize=CHUNK_ROWS):
        if since is not None:
            chunk["timeFrom"] = pd.to_datetime(chunk["timeFrom"], utc=True)
            chunk = chunk[chunk["timeFrom"] >= since]
            if chunk.empty:
                continue
        yield chunk


def aggregate_file(raw_path: Path, since: pd.Timestamp | None = None) -> pd.DataFrame:
    """Stream-aggregate a raw BOALF CSV into one half-hourly DataFrame."""
    parts = []
    n_chunks = 0
    for chunk in iter_chunks(raw_path, since=since):
        parts.append(aggregate_chunk(chunk))
        n_chunks += 1
        if n_chunks % 10 == 0:
            print(f"  ... processed {n_chunks} chunks", flush=True)
    if not parts:
        return pd.DataFrame(columns=["bucket"] + AGG_COLS).set_index("bucket")
    # Same buckets can appear across chunks (period boundaries), so re-group
    merged = pd.concat(parts).groupby(level=0).sum()
    # boalf_max_mw was incorrectly summed by the line above; recompute via max
    # over the per-chunk maxes (max of maxes = overall max).
    merged["boalf_max_mw"] = pd.concat([p["boalf_max_mw"] for p in parts]) \
                                .groupby(level=0).max()
    return merged


def merge_aggregates(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Merge new aggregate rows into the existing aggregates file.

    For overlap windows (buckets present in both), the NEW rows win because
    the incremental pull is more recent and authoritative.
    """
    if existing.empty:
        return new
    if new.empty:
        return existing
    combined = pd.concat([existing, new])
    # Within a duplicated bucket, drop the older row
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["bootstrap", "incremental"], required=True)
    ap.add_argument("--raw",  type=Path, required=True,
                    help="Path to raw BOALF CSV (full file for bootstrap; "
                         "small chunk for incremental)")
    ap.add_argument("--out",  type=Path, required=True,
                    help="Path to write/update boalf_aggregates.csv")
    ap.add_argument("--since", default=None,
                    help="ISO date — keep only raw rows >= this (incremental mode)")
    args = ap.parse_args()

    since_ts = pd.Timestamp(args.since, tz="UTC") if args.since else None

    print(f"Mode: {args.mode}")
    print(f"Raw:  {args.raw}")
    print(f"Out:  {args.out}")
    if since_ts is not None:
        print(f"Since: {since_ts}")

    if not args.raw.exists():
        print(f"FATAL: raw BOALF file not found at {args.raw}")
        sys.exit(1)

    new_agg = aggregate_file(args.raw, since=since_ts)
    print(f"Aggregated {len(new_agg):,} buckets from raw")

    if args.mode == "incremental" and args.out.exists():
        existing = pd.read_csv(args.out, parse_dates=["bucket"])
        existing = existing.set_index("bucket")
        # parse_dates may produce tz-naive — ensure UTC for clean merge
        if existing.index.tz is None:
            existing.index = existing.index.tz_localize("UTC")
        merged = merge_aggregates(existing, new_agg)
        print(f"Merged: was {len(existing):,} rows, now {len(merged):,} "
              f"(+{len(merged) - len(existing):,})")
    else:
        merged = new_agg
        print(f"Bootstrap write: {len(merged):,} rows")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Reset index so bucket becomes a column → simpler CSV reads downstream
    merged.reset_index().to_csv(args.out, index=False)
    size_mb = args.out.stat().st_size / (1024 * 1024)
    print(f"Wrote {args.out}  ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
