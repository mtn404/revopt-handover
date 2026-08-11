"""
Diagnostic: prints what the current Supabase credentials can see.

Run locally to verify your env vars work, then in CI (via workflow_dispatch
trigger) to compare. If the local run shows files but CI doesn't, the GitHub
Actions secrets don't match what you used for the bootstrap.

Usage:
    $env:SUPABASE_URL = "https://xxxx.supabase.co"
    $env:SUPABASE_SERVICE_ROLE_KEY = "eyJ..."
    py -3.13 pipeline/diagnose_supabase.py
"""

import os, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from supabase_io import _client, bucket_name, list_files, PARQUET_FILES


def main():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    print("=" * 60)
    print("SUPABASE DIAGNOSTIC")
    print("=" * 60)
    print(f"SUPABASE_URL              : {url}")
    print(f"SUPABASE_SERVICE_ROLE_KEY : {'set, len=' + str(len(key)) if key else 'NOT SET'}")
    print(f"SUPABASE_BUCKET           : {bucket_name()}")
    print()

    try:
        sb = _client()
    except Exception as ex:
        print(f"FATAL: cannot construct client: {ex}")
        sys.exit(1)

    # Try to list each folder
    for folder in ("parquets", "models_cache", "proprietary", "snapshots"):
        print(f"--- {folder}/ ---")
        try:
            items = sb.storage.from_(bucket_name()).list(
                path=folder, options={"limit": 100}
            )
            if not items:
                print("  (empty or not visible to this key)")
            for it in items:
                size = it.get("metadata", {}).get("size", "?")
                print(f"  {it.get('name'):50s}  {size} bytes")
        except Exception as ex:
            print(f"  ! list failed: {type(ex).__name__}: {ex}")
        print()

    # Try a download of the known-present file
    print("--- Test download: parquets/master.parquet ---")
    try:
        data = sb.storage.from_(bucket_name()).download("parquets/master.parquet")
        print(f"  ✓ downloaded {len(data):,} bytes")
    except Exception as ex:
        print(f"  ! download failed: {type(ex).__name__}: {ex}")


if __name__ == "__main__":
    main()
