"""
One-time bootstrap: uploads the local clean-pipeline parquets + proprietary
inputs to Supabase Storage so the daily cron has a starting state.

Run this from your laptop (NOT from CI) the first time you wire up Supabase:

    export SUPABASE_URL="https://YOUR-PROJECT.supabase.co"
    export SUPABASE_SERVICE_ROLE_KEY="eyJ..."     # from Supabase dashboard
    python pipeline/bootstrap_supabase.py

What it does:
  1. Creates the `bess-mvp` bucket if missing (private, no public access)
  2. Uploads every parquet from ../clean-pipeline/data/processed/
  3. Uploads proprietary inputs from ../clean-pipeline/data/proprietary/
  4. Uploads the current snapshot.json
  5. Writes models_cache/last_retrained.txt with today's date as a sentinel

Idempotent: re-running just re-uploads (upsert). Safe to use to push a
refreshed local state up to the live pipeline.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE       = Path(__file__).resolve().parent   # pipeline/orchestration/
STAGES     = HERE.parent                        # pipeline/
REPO       = STAGES.parent                      # repo root
DATA       = STAGES / "data"
PROCESSED  = DATA / "processed"
PROP       = DATA / "proprietary"
RAW        = DATA / "raw"
INGESTION  = STAGES / "01_ingestion"
SNAPSHOT   = REPO / "data" / "snapshot.json"

# Raw CSV upload size cap — skip BOALF (~2.4 GB) to stay in free tier
RAW_SIZE_CAP_MB = 200

# Local supabase_io import
sys.path.insert(0, str(HERE))
from supabase_io import (  # noqa: E402
    _client, bucket_name, upload, upload_bytes,
    PARQUET_FILES, PROPRIETARY_FILES,
)


def ensure_bucket():
    sb = _client()
    name = bucket_name()
    try:
        buckets = sb.storage.list_buckets()
        existing = [b.id if hasattr(b, "id") else b.get("id") for b in buckets]
        if name in existing:
            print(f"  bucket '{name}' exists")
            return
        sb.storage.create_bucket(name, options={"public": False})
        print(f"  created PRIVATE bucket '{name}'")
    except Exception as ex:
        # Some supabase-py versions raise on create-if-exists; surface but continue
        print(f"  ensure_bucket warning: {ex}")


def upload_parquets():
    print(f"\n=== Uploading parquets from {PROCESSED} ===")
    uploaded = 0
    for f in PARQUET_FILES:
        src = PROCESSED / f
        if not src.exists():
            print(f"  skip {f}: not in local processed/ (will be generated later)")
            continue
        size_mb = src.stat().st_size / (1024 * 1024)
        upload(src, f"parquets/{f}", content_type="application/octet-stream")
        print(f"  ✓ parquets/{f}  ({size_mb:.2f} MB)")
        uploaded += 1
    print(f"  total: {uploaded} parquets uploaded")


def upload_proprietary():
    print(f"\n=== Uploading proprietary inputs from {PROP} ===")
    uploaded = 0
    for src in PROP.iterdir():
        if src.is_dir() or src.name.startswith("."):
            continue
        size_mb = src.stat().st_size / (1024 * 1024)
        # Normalize Spectron filename to Spectron_latest.xlsx so the cron has
        # a stable path; keep the original separately for archival
        if src.name.startswith("Spectron_") and src.name.endswith(".xlsx"):
            upload(src, f"proprietary/{src.name}",
                   content_type="application/octet-stream")
            upload(src, "proprietary/Spectron_latest.xlsx",
                   content_type="application/octet-stream")
            print(f"  ✓ proprietary/{src.name} (+ alias Spectron_latest.xlsx, {size_mb:.2f} MB)")
        else:
            upload(src, f"proprietary/{src.name}",
                   content_type="application/octet-stream")
            print(f"  ✓ proprietary/{src.name}  ({size_mb:.2f} MB)")
        uploaded += 1
    print(f"  total: {uploaded} proprietary files uploaded")


def ensure_boalf_aggregates():
    """If bmrs_boalf.csv exists locally but boalf_aggregates.csv doesn't, run
    aggregate_boalf.py once to produce it. This is a no-op on repeat runs."""
    raw_boalf = RAW / "bmrs_boalf.csv"
    agg_boalf = RAW / "boalf_aggregates.csv"
    if not raw_boalf.exists():
        print("  (no raw BOALF locally — skipping aggregation)")
        return
    if agg_boalf.exists():
        size_mb = agg_boalf.stat().st_size / (1024 * 1024)
        print(f"  boalf_aggregates.csv already present ({size_mb:.2f} MB)")
        return
    print(f"  building boalf_aggregates.csv from {raw_boalf.name} (one-time, ~5 min)…")
    import subprocess, sys as _sys
    res = subprocess.run([
        _sys.executable,
        str(INGESTION / "aggregate_boalf.py"),
        "--mode", "bootstrap",
        "--raw",  str(raw_boalf),
        "--out",  str(agg_boalf),
    ], check=False)
    if res.returncode != 0:
        print(f"  ! aggregator returned {res.returncode}")


def upload_raw():
    print(f"\n=== Uploading raw CSVs from {RAW} (cap {RAW_SIZE_CAP_MB} MB) ===")
    if not RAW.exists():
        print("  ! raw/ directory missing — skipping (run pull_all.py first)")
        return
    # Make sure BOALF aggregates exist before uploading raw/
    ensure_boalf_aggregates()
    uploaded = skipped_big = 0
    for src in sorted(RAW.glob("*.csv")):
        # Never upload the giant raw bmrs_boalf.csv — aggregates replace it
        if src.name == "bmrs_boalf.csv":
            print(f"  ⊘ skip {src.name} (replaced by boalf_aggregates.csv)")
            skipped_big += 1
            continue
        size_mb = src.stat().st_size / (1024 * 1024)
        if size_mb > RAW_SIZE_CAP_MB:
            print(f"  ⊘ skip {src.name} ({size_mb:.0f} MB — too large for free tier)")
            skipped_big += 1
            continue
        upload(src, f"raw/{src.name}", content_type="text/csv")
        print(f"  ✓ raw/{src.name}  ({size_mb:.2f} MB)")
        uploaded += 1
    print(f"  total: {uploaded} raw files uploaded, {skipped_big} skipped")


def upload_snapshot():
    print(f"\n=== Uploading snapshot.json ===")
    if not SNAPSHOT.exists():
        print(f"  ! {SNAPSHOT} not found, skipping")
        return
    today = date.today().isoformat()
    upload(SNAPSHOT, "snapshots/latest.json", content_type="application/json")
    upload(SNAPSHOT, f"snapshots/snapshot_{today}.json", content_type="application/json")
    print(f"  ✓ snapshots/latest.json  +  snapshots/snapshot_{today}.json")


def upload_retrain_sentinel():
    print(f"\n=== Writing models_cache/last_retrained.txt sentinel ===")
    today = date.today().isoformat()
    upload_bytes(today.encode("utf-8"),
                 "models_cache/last_retrained.txt",
                 content_type="text/plain")
    print(f"  ✓ models_cache/last_retrained.txt = {today}")


def main():
    print("=" * 70)
    print(f"BOOTSTRAPPING Supabase bucket: {bucket_name()}")
    print("=" * 70)
    ensure_bucket()
    upload_parquets()
    upload_proprietary()
    upload_raw()
    upload_snapshot()
    upload_retrain_sentinel()
    print("\nDone. Next steps:")
    print("  1. Add SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY to GitHub Actions secrets")
    print("  2. Wait for the next 08:30 UTC cron, or trigger manually:")
    print("     gh workflow run 'Daily snapshot refresh'")


if __name__ == "__main__":
    main()
