"""
Export the rolling 7-day forecast horizon JSON consumed by /forecasts page.

Reads the 4 model prediction parquets + Model A's daily aggregates and
produces ../revopt-mvp/data/forecasts_7day.json with:

  days[]                  per-day daily aggregates for the rolling 7-day horizon
    date, label           (e.g. "2026-06-08", "Mon")
    da_mean/peak/trough   half-hourly DA forecast aggregates
    sbp_mean/peak/trough  half-hourly SBP forecast aggregates
    spike_periods         count of SPs with spike_prob >= 0.5
    ancillary_clearing    per-product daily mean of Model D predictions

  spike_grid_today        48 × {period, time, prob} of today's spike probs

The "horizon" is anchored on the day AFTER the latest LP-solved day
(i.e. tomorrow from the operator's POV at refresh time).
"""

from __future__ import annotations

import sys, json, os
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

HERE  = Path(__file__).resolve().parent
PROC  = HERE.parent / "data" / "processed"

_env_out = os.environ.get("MVP_FORECASTS_OUT", "").strip()
OUT = Path(_env_out) if _env_out else (HERE.parent.parent / "revopt-mvp" / "data" / "forecasts_7day.json")

PRODUCTS = ["dch", "dcl", "dmh", "dml", "drh", "drl", "ffr"]
HORIZON_DAYS = 7


def period_to_time(p: int) -> str:
    """EFA-day SP → wall-clock start time. EFA SP 1 = 23:00 of (date-1)."""
    base = 23 * 60
    mins = (base + (p - 1) * 30) % (24 * 60)
    return f"{mins // 60:02d}:{mins % 60:02d}"


def efa_grid(predictions_df, efa_date, value_col, fill=0.0):
    """Pull a 48-length [(sp 1..48, value)] aligned to the EFA day. SP 1, 2 of
    the EFA day come from calendar (efa_date-1, SP 47, 48); SP 3..48 come
    from calendar (efa_date, SP 1..46)."""
    out = [fill] * 48
    prev = efa_date - pd.Timedelta(days=1)
    sub_prev = predictions_df[predictions_df["date"] == prev] if "date" in predictions_df.columns \
               else predictions_df[predictions_df.get("uk_date") == prev]
    if "sp" not in sub_prev.columns and "period" in sub_prev.columns:
        sub_prev = sub_prev.rename(columns={"period": "sp"})
    if not sub_prev.empty:
        m_prev = {int(r["sp"]): float(r[value_col]) for _, r in sub_prev.iterrows() if pd.notna(r[value_col])}
        for j, sp in enumerate([47, 48]):
            if sp in m_prev: out[j] = m_prev[sp]
    sub_curr = predictions_df[predictions_df["date"] == efa_date] if "date" in predictions_df.columns \
               else predictions_df[predictions_df.get("uk_date") == efa_date]
    if "sp" not in sub_curr.columns and "period" in sub_curr.columns:
        sub_curr = sub_curr.rename(columns={"period": "sp"})
    if not sub_curr.empty:
        m_curr = {int(r["sp"]): float(r[value_col]) for _, r in sub_curr.iterrows() if pd.notna(r[value_col])}
        for j in range(46):
            sp = j + 1
            if sp in m_curr: out[j + 2] = m_curr[sp]
    return out


def main():
    ma = pd.read_parquet(PROC / "model_a_ensemble_predictions.parquet")
    mb = pd.read_parquet(PROC / "model_b_predictions.parquet")
    mc = pd.read_parquet(PROC / "model_c_predictions.parquet")
    md = pd.read_parquet(PROC / "model_d_predictions.parquet")

    ma["date"] = pd.to_datetime(ma["date"])
    md["date"] = pd.to_datetime(md["date"])
    # Model B / C are indexed by UTC timestamp — convert to settlement date
    for d, col_pred, col_actual in [(mb, "predicted_sbp", "actual_sbp"),
                                     (mc, "prob",          "actual")]:
        if not isinstance(d.index, pd.DatetimeIndex):
            d.index = pd.to_datetime(d.index, utc=True)
        d["uk_date"] = d.index.tz_convert("Europe/London").date if d.index.tz else d.index.date
        d["uk_date"] = pd.to_datetime(d["uk_date"])
        d["sp"] = ((d.index.tz_convert("Europe/London").hour if d.index.tz else d.index.hour) * 2 +
                   (d.index.tz_convert("Europe/London").minute if d.index.tz else d.index.minute) // 30) + 1

    # Anchor: pick the most recent 7 days that have FULL (≥ 24 SPs) Model A
    # coverage. Days with sparse predictions (due to BMRS data gaps in the
    # rolling tail) are skipped so the chart never shows £0 placeholder cells.
    MIN_SPS = 24
    sps_per_day = ma.groupby("date").size()
    full_days = sps_per_day[sps_per_day >= MIN_SPS].index.sort_values()
    if len(full_days) >= HORIZON_DAYS:
        horizon_days = full_days[-HORIZON_DAYS:]
    else:
        horizon_days = full_days
    latest_a_day = ma["date"].max()
    sparse_count = (sps_per_day < MIN_SPS).sum()
    print(f"Latest Model A day: {latest_a_day.date()} "
          f"(skipped {sparse_count} sparse days from the rolling tail)")
    if len(horizon_days) == 0:
        print("No full-coverage days in the predictions parquet — aborting")
        sys.exit(1)
    print(f"Horizon: {horizon_days[0].date()} → {horizon_days[-1].date()}")

    today_label_day = horizon_days[-1]  # rightmost = "today" in the display

    days = []
    for d in horizon_days:
        # EFA-day aggregates from Model A ensemble (date d represents the EFA
        # day ending at 23:00 of d; it spans 23:00 (d-1) to 23:00 d).
        a_day_arr = efa_grid(ma, d, "predicted", fill=float("nan"))
        a_day = pd.Series([v for v in a_day_arr if not pd.isna(v)])
        sbp_day_arr = efa_grid(mb, d, "predicted_sbp", fill=float("nan")) if "uk_date" in mb.columns else []
        sbp_day = pd.Series([v for v in sbp_day_arr if not pd.isna(v)])
        spike_day_arr = efa_grid(mc, d, "prob", fill=float("nan")) if "uk_date" in mc.columns else []
        spike_day_prob = pd.Series([v for v in spike_day_arr if not pd.isna(v)])
        # Per-product ancillary daily mean
        md_day = md[md["date"] == d]
        anc = {}
        for p in PRODUCTS:
            sub = md_day[md_day["product"] == p]["predicted"] if not md_day.empty else pd.Series(dtype=float)
            anc[p] = round(float(sub.mean()), 1) if len(sub) else 0.0

        def safe(series, agg, default=0.0):
            if len(series) == 0: return default
            val = getattr(series, agg)()
            return round(float(val), 1) if pd.notna(val) else default

        # Label format: short date + weekday (e.g. "27 May Wed").
        # We DO NOT use "Today" — the picked horizon is the most recent days with
        # FULL coverage, which may not include today's calendar date when BMRS
        # data is still settling. Always show the explicit date so the user can
        # see which actual days are in the chart.
        label = d.strftime("%-d %b") if sys.platform != "win32" else d.strftime("%#d %b")
        days.append({
            "date": d.date().isoformat(),
            "label": label,
            "weekday": d.strftime("%a"),
            "da_mean":   safe(a_day,   "mean"),
            "da_peak":   safe(a_day,   "max"),
            "da_trough": safe(a_day,   "min"),
            "sbp_mean":  safe(sbp_day, "mean"),
            "sbp_peak":  safe(sbp_day, "max"),
            "sbp_trough":safe(sbp_day, "min"),
            "spike_periods": int((spike_day_prob.fillna(0) >= 0.5).sum()),
            "ancillary_clearing": anc,
        })

    # Today's per-SP spike grid (48 entries) — EFA-day ordered: SP 1 = 23:00.
    spike_grid = []
    if "uk_date" in mc.columns:
        # The efa_grid helper expects a "date" column; rename for the call.
        mc_renamed = mc.rename(columns={"uk_date": "date"})
        grid_vals = efa_grid(mc_renamed, today_label_day, "prob", fill=0.0)
    else:
        grid_vals = [0.0] * 48
    for sp_idx, prob in enumerate(grid_vals):
        sp = sp_idx + 1
        spike_grid.append({"period": sp, "time": period_to_time(sp), "prob": round(float(prob), 3)})

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "horizon_days": HORIZON_DAYS,
        "anchor_date":  today_label_day.date().isoformat(),
        "days":         days,
        "spike_grid_today": spike_grid,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}  ({OUT.stat().st_size // 1024} KB · {len(days)} days · "
          f"spike grid {len(spike_grid)} SPs)")


if __name__ == "__main__":
    main()
