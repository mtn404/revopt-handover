"""
Supabase Storage helpers — the persistence layer for the live pipeline.

All large pipeline state lives in a single private Supabase bucket
(default name `bess-mvp`). Layout:

    bess-mvp/
      parquets/
        master.parquet                      ← rolling master, updated daily
        lp_v6_ensemble_dispatch.parquet
        lp_v6_ensemble_revenue.parquet
        lp_v6_ensemble_anc.parquet
        pf_v6_dispatch.parquet
        pf_v6_revenue.parquet
        model_a_ensemble_predictions.parquet
        model_b_predictions.parquet
        model_c_predictions.parquet
        model_d_predictions.parquet
      models_cache/
        model_a_lear.pkl                    ← updated monthly by retrain job
        model_a_lgbm_extras.txt
        model_b.json
        model_c.json
        model_d.json
        last_retrained.txt                  ← sentinel: ISO date string
      proprietary/
        Spectron_latest.xlsx                ← drop-zone for manual uploads
        uka_daily_interpolated.csv
      snapshots/
        snapshot_YYYY-MM-DD.json            ← archival copies (daily)
        latest.json                         ← always the most recent

Auth: requires two environment variables, set in GitHub Actions secrets:
  * SUPABASE_URL                e.g. https://abc123def456.supabase.co
  * SUPABASE_SERVICE_ROLE_KEY   long JWT string (not the anon key!)

The service-role key bypasses Row Level Security. It MUST NOT be
embedded in any client-side code and MUST NOT be committed to git.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

try:
    from supabase import create_client, Client
except ImportError as ex:
    print(f"supabase-py not installed: {ex}", file=sys.stderr)
    raise

BUCKET_DEFAULT = "bess-mvp"


def _client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set. "
            "Locally: source pipeline/.env  |  CI: configured as GH Actions secrets."
        )
    return create_client(url, key)


def bucket_name() -> str:
    # `or` (not the get-default) so an explicitly-empty env var still falls back.
    # GitHub Actions sets env vars to "" when the referenced var/secret is missing.
    return os.environ.get("SUPABASE_BUCKET") or BUCKET_DEFAULT


# ---------------------------------------------------------------------------
# Core ops
# ---------------------------------------------------------------------------
def list_files(prefix: str, recursive: bool = True) -> list[dict]:
    """List objects under a prefix. Returns the raw Supabase list response."""
    sb = _client()
    items = sb.storage.from_(bucket_name()).list(
        path=prefix, options={"limit": 1000, "sortBy": {"column": "name", "order": "asc"}}
    )
    if not recursive:
        return items
    # Manually recurse into subfolders (Supabase list is non-recursive)
    out = []
    for it in items:
        if it.get("id") is None:  # folder marker
            sub = list_files(f"{prefix.rstrip('/')}/{it['name']}", recursive=True)
            out.extend(sub)
        else:
            out.append({**it, "_full_path": f"{prefix.rstrip('/')}/{it['name']}"})
    return out


def download(remote_path: str, local_path: Path) -> bool:
    """Download one object. Returns True on success, False if the object is missing.
    Logs the real exception class + message on first failure so 401/403 don't
    silently look like 404s."""
    sb = _client()
    try:
        data = sb.storage.from_(bucket_name()).download(remote_path)
    except Exception as ex:
        msg = str(ex).lower()
        if "not found" in msg or "404" in msg or "object not found" in msg:
            return False
        # Anything else (401, 403, 5xx, network) is a real problem — log loudly
        # but DON'T crash the cron; the orchestrator falls back to backtest mode.
        print(f"  ! download({remote_path}): {type(ex).__name__}: {ex}",
              flush=True)
        return False
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(data)
    return True


_UPLOAD_RETRIES = 4   # total attempts
_UPLOAD_BACKOFF = 5.0  # base seconds; 5, 10, 20, 40 with exponential


def _upload_with_retry(body: bytes, remote_path: str, content_type: str | None) -> None:
    """Upload with exponential backoff. Large parquets (master.parquet ~16 MB)
    occasionally hit Supabase's per-request read timeout when the network is
    congested; a simple retry resolves these transients."""
    import time as _time
    sb = _client()
    opts = {"upsert": "true"}
    if content_type:
        opts["content-type"] = content_type
    last_exc: Exception | None = None
    for attempt in range(1, _UPLOAD_RETRIES + 1):
        try:
            sb.storage.from_(bucket_name()).upload(remote_path, body, file_options=opts)
            if attempt > 1:
                print(f"    upload({remote_path}): succeeded on attempt {attempt}", flush=True)
            return
        except Exception as ex:
            last_exc = ex
            backoff = _UPLOAD_BACKOFF * (2 ** (attempt - 1))
            print(f"    upload({remote_path}): attempt {attempt}/{_UPLOAD_RETRIES} failed "
                  f"({type(ex).__name__}: {ex}); retrying in {backoff:.0f}s",
                  flush=True)
            _time.sleep(backoff)
            # Reset client in case the underlying httpx connection is bad
            try:
                _reset_client()
            except NameError:
                pass
    # All retries exhausted
    raise last_exc if last_exc else RuntimeError("upload failed without exception")


def upload(local_path: Path, remote_path: str, content_type: str | None = None) -> None:
    """Upload one object, replacing if it already exists (upsert)."""
    if not local_path.exists():
        raise FileNotFoundError(local_path)
    body = local_path.read_bytes()
    _upload_with_retry(body, remote_path, content_type)


def upload_bytes(data: bytes, remote_path: str, content_type: str | None = None) -> None:
    """Upload an in-memory blob (no local file)."""
    _upload_with_retry(data, remote_path, content_type)


# ---------------------------------------------------------------------------
# Convenience helpers used by refresh_snapshot.py
# ---------------------------------------------------------------------------
PARQUET_FILES = [
    "master.parquet",
    "lp_v6_ensemble_dispatch.parquet",
    "lp_v6_ensemble_revenue.parquet",
    "lp_v6_ensemble_anc.parquet",
    "pf_v6_dispatch.parquet",
    "pf_v6_revenue.parquet",
    "model_a_ensemble_predictions.parquet",
    "model_b_predictions.parquet",
    "model_c_predictions.parquet",
    "model_d_predictions.parquet",
    # NB: acceptance_rates*.parquet are STATIC lookup tables committed to git,
    # NOT pipeline outputs. They were incorrectly listed here on 2026-07-10,
    # causing Supabase pull to overwrite fresh git-committed versions with
    # stale ones. Removed 2026-07-13. LP + PF now read the git-checked-out
    # versions directly (see LP_ACCEPTANCE_TIER env var).
]

MODEL_FILES = [
    "model_a_lear.pkl",
    "model_a_lgbm_extras.txt",
    "model_b.json",
    "model_c.json",
    "model_d.json",
    "last_retrained.txt",
]

PROPRIETARY_FILES = [
    "DA_Prices.xlsx",
    "uka_daily_interpolated.csv",
    "Spectron_Jan22_to_May26.xlsx",   # forward-curves file used by build_master Phase 11
    "Spectron_Nov25_to_Apr26.xlsx",   # newer forward-curves file (optional, used if present)
    "Spectron_latest.xlsx",           # alias of most recent Spectron upload
]


def sync_down(local_dir: Path, remote_prefix: str, filenames: Iterable[str]) -> dict:
    """Download a known list of files. Returns {filename: bool} indicating presence."""
    local_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    for f in filenames:
        ok = download(f"{remote_prefix}/{f}", local_dir / f)
        result[f] = ok
        if not ok:
            print(f"  sync_down: missing {remote_prefix}/{f} (will be created later)")
    return result


def sync_up(local_dir: Path, remote_prefix: str, filenames: Iterable[str]) -> dict:
    """Upload a known list of files (skips ones that don't exist locally)."""
    result = {}
    for f in filenames:
        local = local_dir / f
        if not local.exists():
            result[f] = False
            continue
        ct = "application/octet-stream"
        if f.endswith(".json"): ct = "application/json"
        elif f.endswith(".txt"): ct = "text/plain"
        elif f.endswith(".csv"): ct = "text/csv"
        upload(local, f"{remote_prefix}/{f}", content_type=ct)
        result[f] = True
    return result
