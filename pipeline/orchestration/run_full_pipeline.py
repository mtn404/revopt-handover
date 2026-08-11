"""
Full pipeline run — invoked weekly by a GitHub Action to refresh ALL
predictions, LP/PF dispatch, and the dashboard snapshot.

End-to-end:
  1.  Sync state from Supabase (master.parquet, prediction parquets,
      proprietary inputs, raw CSVs)
  2.  Pull last 14 days of public data (incremental)
  3.  Set LIVE_TEST_END env var so vendored scripts know when to stop
  4.  Run vendored scripts in dependency order:
        - 02_master/build_master.py             rebuild master.parquet
        - 03_models/retrain_model_a_lear.py
        - 03_models/retrain_model_a_lgbm.py
        - 03_models/ensemble_model_a.py
        - 03_models/retrain_model_b.py
        - 03_models/retrain_model_c.py
        - 03_models/retrain_model_d.py
        - 04_optimiser/lp_v6_ensemble.py
        - 04_optimiser/compute_pf_v6.py
        - 05_evaluation/export_to_mvp_snapshot.py  writes data/snapshot.json
  5.  Stamp snapshot.json: pipeline_mode = "live", last_retrained = today
  6.  Push refreshed state back to Supabase
  7.  Exit so the calling workflow can git-commit the new snapshot.

Runs in ~45-70 minutes. Designed for the WEEKLY workflow, not the daily.

Env vars (required):
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""

from __future__ import annotations

import json, os, subprocess, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# Layout (after restructure):
#   pipeline/
#     orchestration/   ← this file lives here
#     01_ingestion/    ← pull_incremental, pull_phase2_features, aggregate_boalf
#     02_master/       ← build_master
#     03_models/       ← model A/B/C/D retrain + ensemble
#     04_optimiser/    ← LP v6 + PF v6
#     05_evaluation/   ← snapshot + forecast exporters
#     data/            ← raw/, processed/, proprietary/  (gitignored runtime)
HERE             = Path(__file__).resolve().parent          # pipeline/orchestration/
STAGES           = HERE.parent                              # pipeline/
REPO_ROOT        = STAGES.parent                            # repo root
CP_DATA          = STAGES / "data"
CP_RAW           = CP_DATA / "raw"
CP_PROCESSED     = CP_DATA / "processed"
CP_PROPRIETARY   = CP_DATA / "proprietary"
SNAPSHOT         = REPO_ROOT / "data" / "snapshot.json"

INGESTION        = STAGES / "01_ingestion"

# Where pull_incremental.py writes (separate from vendored, copied to CP_RAW)
RAW_INCREMENTAL  = CP_RAW

# Python interpreter to use for vendored scripts
PY = sys.executable

t0 = time.time()
def log(m): print(f"[{(time.time()-t0):7.1f}s] {m}", flush=True)


def have_supabase_creds() -> bool:
    return bool(os.environ.get("SUPABASE_URL")
                and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def step_setup_dirs():
    """Make sure all needed directories exist."""
    for d in (CP_RAW, CP_PROCESSED, CP_PROPRIETARY, RAW_INCREMENTAL):
        d.mkdir(parents=True, exist_ok=True)


def step_supabase_pull():
    """Download persisted state from Supabase → pipeline/data/*."""
    log("STEP 1: pull state from Supabase")
    if not have_supabase_creds():
        log("  ! no Supabase creds — running on local state only")
        return
    sys.path.insert(0, str(HERE))
    from supabase_io import sync_down, PARQUET_FILES, PROPRIETARY_FILES, list_files

    # Parquets → CP_PROCESSED
    res_pq = sync_down(CP_PROCESSED, "parquets", PARQUET_FILES)
    log(f"  parquets : {sum(res_pq.values())}/{len(PARQUET_FILES)}")

    # Proprietary → CP_PROPRIETARY
    res_pr = sync_down(CP_PROPRIETARY, "proprietary", PROPRIETARY_FILES)
    log(f"  proprietary: {sum(res_pr.values())}/{len(PROPRIETARY_FILES)}")

    # Raw CSVs → CP_RAW (use whatever's currently uploaded under raw/)
    try:
        items = list_files("raw", recursive=False)
        n = 0
        for it in items:
            name = it.get("name")
            if not name or it.get("id") is None:
                continue
            from supabase_io import download
            ok = download(f"raw/{name}", CP_RAW / name)
            if ok: n += 1
        log(f"  raw CSVs : {n} files")
    except Exception as ex:
        log(f"  raw CSVs : (none uploaded yet — {ex})")


def step_pull_incremental():
    """Run pull_incremental.py to grab last 30 days into CP_RAW.

    Widened from 14 → 30 days on 2026-07-02 to self-heal coverage holes
    left by the earlier single-source wind endpoint bug (June 11-18 gap).
    30 days of overlap ensures any intermediate missing day is backfilled
    on the next run without needing a targeted manual repair."""
    log("STEP 2a: pull last 30 days of public market data")
    env = os.environ.copy()
    env["PIPELINE_RAW_DIR"] = str(CP_RAW)
    res = subprocess.run(
        [PY, str(INGESTION / "pull_incremental.py"), "--days", "30"],
        env=env, check=False
    )
    if res.returncode != 0:
        log(f"  ! pull_incremental returned {res.returncode}")

    # Phase 2 supplementary features (interconnector net flow + extended
    # system-prices). Run after the main pull so the same window aligns.
    p2_script = INGESTION / "pull_phase2_features.py"
    if p2_script.exists():
        log("STEP 2a-ii: pull Phase 2 features (IC flows + BSAD adjustments)")
        res2 = subprocess.run(
            [PY, str(p2_script), "--days", "30"],
            env=env, check=False
        )
        if res2.returncode != 0:
            log(f"  ! pull_phase2_features returned {res2.returncode} — continuing")


def step_refresh_boalf():
    """Pull last 30 days of raw BOALF, aggregate, merge into the existing
    boalf_aggregates.csv. The raw chunk is discarded after aggregation.
    Widened to 30 days on 2026-07-02 to keep BOALF window aligned with
    the main incremental pull."""
    log("STEP 2b: refresh BOALF aggregates")

    from datetime import datetime as _dt, timedelta as _td
    yesterday = _dt.now(timezone.utc).date() - _td(days=1)
    start     = yesterday - _td(days=29)

    # Pull raw BOALF for the window
    sys.path.insert(0, str(INGESTION))
    from pull_incremental import pull_bmrs_boalf
    # pull_incremental writes to PIPELINE_RAW_DIR (=CP_RAW)
    os.environ["PIPELINE_RAW_DIR"] = str(CP_RAW)
    raw_chunk = pull_bmrs_boalf(start, yesterday, out_name="bmrs_boalf_incremental.csv")
    if raw_chunk is None:
        log("  no new BOALF rows pulled — leaving aggregates unchanged")
        return

    # Run the aggregator in incremental mode
    agg_path = CP_RAW / "boalf_aggregates.csv"
    cmd = [PY, str(INGESTION / "aggregate_boalf.py"),
           "--mode", "incremental",
           "--raw",  str(raw_chunk),
           "--out",  str(agg_path),
           "--since", start.isoformat()]
    res = subprocess.run(cmd, check=False)
    if res.returncode != 0:
        log(f"  ! aggregator returned {res.returncode}")
        return

    # Discard the raw 14-day chunk — only the aggregates need to persist
    try:
        raw_chunk.unlink()
        log(f"  ✓ discarded raw chunk {raw_chunk.name}")
    except OSError as ex:
        log(f"  ! could not delete raw chunk: {ex}")


def pick_live_end_date() -> str:
    """Decide which date to treat as the LP's TEST_END.

    We use TOMORROW (UTC). With the operationally-safe lag-96 features in
    Model B/C and Model A's day-ahead forecast inputs, predictions for
    tomorrow are computable from data ≥ 2 days old (guaranteed settled).
    So the LP can iterate up to tomorrow and emit dispatch + ancillary
    bids for the next gate closure. Revenue scoring still only happens
    on days with fully-settled actuals.
    """
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    return tomorrow.isoformat()


def run_step(name: str, script_rel: str, env: dict) -> bool:
    """Invoke a vendored script as a subprocess."""
    script = STAGES / script_rel
    log(f"  ► {name}  ({script.relative_to(REPO_ROOT)})")
    t_start = time.time()
    res = subprocess.run([PY, str(script)], env=env, check=False)
    dt = time.time() - t_start
    if res.returncode != 0:
        log(f"    ✗ FAILED in {dt:.1f}s (exit {res.returncode})")
        return False
    log(f"    ✓ done in {dt:.1f}s")
    return True


def step_run_pipeline(live_end: str) -> bool:
    """Run all vendored scripts in dependency order."""
    log(f"STEP 3: run vendored pipeline with LIVE_TEST_END={live_end}")
    env = os.environ.copy()
    env["LIVE_TEST_END"] = live_end
    env["PYTHONUTF8"] = "1"
    # Override the vendored exporters' output paths so writes land in data/
    env["MVP_SNAPSHOT_OUT"]  = str(SNAPSHOT)
    env["MVP_FORECASTS_OUT"] = str(REPO_ROOT / "data" / "forecasts_7day.json")
    sequence = [
        ("build master",     "02_master/build_master.py"),
        ("model A — LEAR",   "03_models/retrain_model_a_lear.py"),
        ("model A — LGBM",   "03_models/retrain_model_a_lgbm.py"),
        ("model A ensemble", "03_models/ensemble_model_a.py"),
        ("model B — SBP",    "03_models/retrain_model_b.py"),
        ("model C — spike",  "03_models/retrain_model_c.py"),
        ("model D — anc",    "03_models/retrain_model_d.py"),
        ("LP v6 ensemble",   "04_optimiser/lp_v6_ensemble.py"),
        ("PF v6 oracle",     "04_optimiser/compute_pf_v6.py"),
        ("export snapshot",      "05_evaluation/export_to_mvp_snapshot.py"),
        ("export 7-day forecasts","05_evaluation/export_forecasts_7day.py"),
    ]
    for name, rel in sequence:
        if not run_step(name, rel, env):
            return False
    return True


def step_relocate_snapshot():
    """The vendored exporter writes to REPO_ROOT/data/snapshot.json
    (or the MVP_SNAPSHOT_OUT env var if set). Verify it landed."""
    log("STEP 4: verify snapshot.json was written")
    if not SNAPSHOT.exists():
        log(f"  ! {SNAPSHOT} not found after export")
        return False
    size_kb = SNAPSHOT.stat().st_size / 1024
    log(f"  ✓ snapshot.json present ({size_kb:.1f} KB)")
    return True


def step_stamp_live(live_end: str):
    """Mark snapshot as LIVE + record the retrain date.

    Note: `last_data_through` is preserved from whatever the exporter wrote
    (which uses the ACTUAL latest day in the LP revenue parquet). Don't
    overwrite it with `live_end` — the LP may have skipped the last day if
    BMRS hadn't published its actuals when the cron ran.
    """
    log("STEP 5: stamp snapshot as LIVE")
    if not SNAPSHOT.exists():
        return
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    snap.setdefault("freshness", {})
    snap["freshness"]["last_refreshed_utc"] = now_utc
    snap["freshness"]["last_retrained"]    = datetime.now(timezone.utc).date().isoformat()
    snap["freshness"]["pipeline_mode"]     = "live"
    # Preserve last_data_through from the exporter (real LP latest day);
    # only fill in if the exporter didn't set it.
    snap["freshness"].setdefault("last_data_through", live_end)
    SNAPSHOT.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    log(f"  ✓ pipeline_mode=live, last_retrained={snap['freshness']['last_retrained']}, "
        f"data_through={snap['freshness']['last_data_through']}")


def _trim_csv_to_window(path: Path, days: int = 365) -> tuple[int, int]:
    """If `path` is too big for Supabase's 50 MB file limit, trim it to keep
    only rows whose date column falls within the last `days` days. Returns
    (rows_before, rows_after). No-op if file is already small enough."""
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
    except OSError:
        return (0, 0)
    if size_mb < 40:
        return (0, 0)  # safely under the 50 MB Supabase limit
    import pandas as pd
    try:
        df = pd.read_csv(path)
    except Exception as ex:
        log(f"    ! couldn't read {path.name} for trim: {ex}")
        return (0, 0)
    before = len(df)
    # Find a date-shaped column to filter on
    date_col = None
    for candidate in ("startTime", "deliveryStart", "from", "date",
                      "targetDate", "Delivery Date", "settlementDate"):
        if candidate in df.columns:
            date_col = candidate
            break
    if date_col is None:
        log(f"    ! no date column found in {path.name}; can't trim")
        return (before, before)
    cutoff = pd.Timestamp.utcnow() - pd.Timedelta(days=days)
    df[date_col] = pd.to_datetime(df[date_col], utc=True, errors="coerce")
    df = df[df[date_col] >= cutoff].copy()
    df.to_csv(path, index=False)
    return (before, len(df))


def step_supabase_push():
    log("STEP 6: push refreshed state to Supabase")
    if not have_supabase_creds():
        log("  ! no Supabase creds — skipping upload")
        return
    sys.path.insert(0, str(HERE))
    from supabase_io import sync_up, upload, PARQUET_FILES, PROPRIETARY_FILES
    n_pq = sum(sync_up(CP_PROCESSED, "parquets",    PARQUET_FILES).values())
    n_pr = sum(sync_up(CP_PROPRIETARY, "proprietary", PROPRIETARY_FILES).values())
    log(f"  parquets   : {n_pq}/{len(PARQUET_FILES)} uploaded")
    log(f"  proprietary: {n_pr}/{len(PROPRIETARY_FILES)} uploaded")
    # Upload raw CSVs — trim those that have grown too large for Supabase
    # (50 MB file limit on free tier). Keeping ~1 year of data is more than
    # enough for lag-336 (1-week) features + walk-forward training context.
    n_raw = n_trimmed = 0
    for csv in CP_RAW.glob("*.csv"):
        if csv.stat().st_size > 500_000_000:    # skip BOALF (~2.4 GB) — too big
            continue
        before, after = _trim_csv_to_window(csv, days=365)
        if before > 0:
            log(f"    trimmed {csv.name}: {before:,} → {after:,} rows "
                f"({csv.stat().st_size / (1024*1024):.1f} MB)")
            n_trimmed += 1
        try:
            upload(csv, f"raw/{csv.name}", content_type="text/csv")
            n_raw += 1
        except Exception as ex:
            log(f"    ! upload failed for {csv.name}: {ex}")
    log(f"  raw CSVs   : {n_raw} uploaded ({n_trimmed} trimmed)")
    # Always push snapshot
    if SNAPSHOT.exists():
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        upload(SNAPSHOT, "snapshots/latest.json", content_type="application/json")
        upload(SNAPSHOT, f"snapshots/snapshot_{today}.json",
               content_type="application/json")
        log(f"  snapshot   : pushed latest + dated")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log("=" * 78)
    log(f"FULL PIPELINE RUN  {datetime.now(timezone.utc).isoformat()}")
    log("=" * 78)

    step_setup_dirs()
    step_supabase_pull()
    step_pull_incremental()
    step_refresh_boalf()

    live_end = pick_live_end_date()
    ok = step_run_pipeline(live_end)
    if not ok:
        log("Pipeline FAILED — leaving snapshot untouched, NOT marking as live")
        sys.exit(1)

    if not step_relocate_snapshot():
        sys.exit(1)
    step_stamp_live(live_end)
    step_supabase_push()

    log(f"DONE in {(time.time()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()
