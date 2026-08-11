"""
Daily snapshot refresh — runs in GitHub Actions.

End-to-end flow:
  1. Download persisted state from Supabase Storage:
       - parquets/master.parquet, lp_*/pf_* revenue+dispatch, model_*/predictions
       - models_cache/* (fitted models for inference; sentinel last_retrained.txt)
       - proprietary/* (Spectron NBP gas, UKA carbon, DA_Prices.xlsx)
  2. Pull last 14 days of public data via pull_incremental.py
  3. If cached models + parquets exist: rebuild master tail, run inference,
     solve LP for today, append to dispatch/revenue parquets, re-export
     snapshot.json with pipeline_mode = "live".
  4. Otherwise: stamp pipeline_mode = "backtest" and leave snapshot content
     untouched (so the dashboard keeps showing the frozen backtest).
  5. Upload changed parquets + snapshot.json back to Supabase.

Required env vars (set as GH Actions secrets):
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  SUPABASE_BUCKET           (optional, defaults to "bess-mvp")
"""

import json, os, sys, subprocess, traceback
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE        = Path(__file__).resolve().parent     # pipeline/orchestration/
STAGES      = HERE.parent                          # pipeline/
REPO_ROOT   = STAGES.parent                        # repo root
SNAPSHOT    = REPO_ROOT / "data" / "snapshot.json"
DATA        = STAGES / "data"
PROCESSED   = DATA / "processed"
MODELS      = DATA / "models_cache"
PROP        = DATA / "proprietary"
INGESTION   = STAGES / "01_ingestion"

# Required artifacts for full LIVE mode
REQUIRED_MODELS = [
    "model_a_lear.pkl",
    "model_a_lgbm_extras.txt",
    "model_b.json",
    "model_c.json",
    "model_d.json",
]
REQUIRED_PARQUETS = [
    "master.parquet",
    "lp_v6_ensemble_revenue.parquet",
    "lp_v6_ensemble_dispatch.parquet",
]


def log(m): print(m, flush=True)


# ---------------------------------------------------------------------------
# Supabase plumbing — wrapped so the script still works if creds are missing
# (useful for local smoke tests without a Supabase project).
# ---------------------------------------------------------------------------
def have_supabase_creds() -> bool:
    return bool(os.environ.get("SUPABASE_URL")
                and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))


def step_supabase_pull() -> dict:
    log("STEP 0: pull persisted state from Supabase")
    # Print what env vars we actually see (URL is non-sensitive, key length tells
    # us if the secret was saved/truncated without leaking the value)
    url = os.environ.get("SUPABASE_URL", "<unset>")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    bkt = os.environ.get("SUPABASE_BUCKET", "<default: bess-mvp>")
    log(f"  env SUPABASE_URL     = {url}")
    log(f"  env SUPABASE_*_KEY   = len={len(key)} (expected 219 for legacy service_role JWT)")
    log(f"  env SUPABASE_BUCKET  = {bkt}")
    if not have_supabase_creds():
        log("  SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — skipping")
        return {}
    sys.path.insert(0, str(HERE))
    from supabase_io import sync_down, PARQUET_FILES, MODEL_FILES, PROPRIETARY_FILES
    res = {}
    res["parquets"] = sync_down(PROCESSED, "parquets", PARQUET_FILES)
    res["models"]   = sync_down(MODELS,    "models_cache", MODEL_FILES)
    res["proprietary"] = sync_down(PROP,   "proprietary", PROPRIETARY_FILES)
    n_p = sum(res["parquets"].values())
    n_m = sum(res["models"].values())
    n_x = sum(res["proprietary"].values())
    log(f"  pulled: {n_p}/{len(PARQUET_FILES)} parquets, "
        f"{n_m}/{len(MODEL_FILES)} models, "
        f"{n_x}/{len(PROPRIETARY_FILES)} proprietary")
    return res


def step_supabase_push():
    log("STEP 5: push refreshed state to Supabase")
    if not have_supabase_creds():
        log("  SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — skipping")
        return
    sys.path.insert(0, str(HERE))
    from supabase_io import sync_up, upload, PARQUET_FILES, MODEL_FILES
    n_p = sum(sync_up(PROCESSED, "parquets", PARQUET_FILES).values())
    n_m = sum(sync_up(MODELS,    "models_cache", MODEL_FILES).values())
    log(f"  pushed: {n_p} parquets, {n_m} models")
    # Always push snapshot (it has the new last_refreshed timestamp)
    if SNAPSHOT.exists():
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        upload(SNAPSHOT, "snapshots/latest.json", content_type="application/json")
        upload(SNAPSHOT, f"snapshots/snapshot_{today}.json",
               content_type="application/json")
        log(f"  pushed: snapshots/latest.json + snapshots/snapshot_{today}.json")


# ---------------------------------------------------------------------------
# Pipeline gates
# ---------------------------------------------------------------------------
def have_cached_models() -> bool:
    missing = [f for f in REQUIRED_MODELS if not (MODELS / f).exists()]
    if missing:
        log(f"  cached models: MISSING {missing}")
        return False
    log("  cached models: OK")
    return True


def have_fresh_parquets() -> bool:
    missing = [f for f in REQUIRED_PARQUETS if not (PROCESSED / f).exists()]
    if missing:
        log(f"  fresh parquets: MISSING {missing}")
        return False
    log("  fresh parquets: OK")
    return True


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def step_pull_public() -> bool:
    log("STEP 1: pull last 14 days of public data (BMRS + NESO + CI)")
    try:
        subprocess.check_call(
            [sys.executable, str(INGESTION / "pull_incremental.py"), "--days", "14"]
        )
        return True
    except Exception as ex:
        log(f"  pull failed: {ex}")
        return False


def step_run_live_pipeline() -> bool:
    """Hook for the live inference + LP solve. Stubbed for now — the daily
    solver script is the last remaining piece. See pipeline/README.md."""
    log("STEP 2: run live pipeline (master rebuild → inference → LP solve)")
    log("  ! live solver not yet implemented — staying in BACKTEST mode")
    # When implemented, this will:
    #   1. Update master.parquet tail with last 14 days of fresh data
    #   2. Load cached models from models_cache/, predict for tomorrow
    #   3. Solve LP for next 7 days, commit Day 1 to dispatch/revenue parquets
    #   4. Run 05_evaluation/export_to_mvp_snapshot.py to refresh snapshot.json
    return False


def step_stamp_freshness(mode: str):
    """Touch ONLY the last_refreshed_utc timestamp.

    Critically, we do NOT change pipeline_mode or last_retrained here. Those
    are set by the WEEKLY full pipeline (run_full_pipeline.py → step_stamp_live)
    and would be wrong if overwritten by this daily job. The daily job exists
    to keep the freshness clock ticking between weekly runs — not to redo or
    override the weekly's mode-decision logic.
    """
    log(f"STEP 4: stamp freshness (refresh time only; preserve mode='{mode}' if present)")
    if not SNAPSHOT.exists():
        log(f"  ! {SNAPSHOT} not found; nothing to stamp")
        return
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    snap.setdefault("freshness", {})
    snap["freshness"]["last_refreshed_utc"] = now_utc
    # Only set pipeline_mode if it's not already set (i.e., on a brand-new
    # snapshot before any weekly run has produced one)
    if "pipeline_mode" not in snap["freshness"]:
        snap["freshness"]["pipeline_mode"] = mode
    SNAPSHOT.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    existing_mode = snap["freshness"].get("pipeline_mode", "(none)")
    log(f"  ✓ stamped refresh time {now_utc} (preserved pipeline_mode={existing_mode})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log("=" * 70)
    log(f"DAILY REFRESH  {datetime.now(timezone.utc).isoformat()}")
    log("=" * 70)

    # 0. Pull persistent state from Supabase
    step_supabase_pull()

    # 1. Pull fresh public market data
    pulled = step_pull_public()

    # 2. Decide whether we can actually do a live solve this run
    can_live = pulled and have_cached_models() and have_fresh_parquets()

    # 3. Run the live solve if possible
    ran_live = False
    if can_live:
        ran_live = step_run_live_pipeline()
    mode = "live" if ran_live else "backtest"

    # 4. Stamp freshness regardless
    step_stamp_freshness(mode)

    # 5. Push everything back to Supabase
    step_supabase_push()

    log("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(0)  # Don't crash the cron — log and continue.
