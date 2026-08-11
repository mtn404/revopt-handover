"""
Convert clean-pipeline outputs into the MVP dashboard's snapshot.json schema.

Output: ../revopt-mvp/data/snapshot.json
"""
import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

HERE  = Path(__file__).resolve().parent
PROC  = HERE.parent / "data" / "processed"
# Output path overridable via env var. run_full_pipeline.py sets it to the
# MVP repo's data/snapshot.json regardless of the vendored layout.
import os as _os
_env_out = _os.environ.get("MVP_SNAPSHOT_OUT", "").strip()
OUT   = Path(_env_out) if _env_out else (HERE.parent.parent / "revopt-mvp" / "data" / "snapshot.json")

# Load clean pipeline outputs
lp_rev    = pd.read_parquet(PROC / "lp_v6_ensemble_revenue.parquet",   engine="fastparquet")
pf_rev    = pd.read_parquet(PROC / "pf_v6_revenue.parquet",             engine="fastparquet")
lp_disp   = pd.read_parquet(PROC / "lp_v6_ensemble_dispatch.parquet",   engine="fastparquet")
lp_anc    = pd.read_parquet(PROC / "lp_v6_ensemble_anc.parquet",        engine="fastparquet")
model_a   = pd.read_parquet(PROC / "model_a_ensemble_predictions.parquet", engine="fastparquet")
model_d   = pd.read_parquet(PROC / "model_d_predictions.parquet",       engine="fastparquet")

for d in (lp_rev, pf_rev, lp_disp, lp_anc, model_a, model_d):
    if "date" in d.columns:
        d["date"] = pd.to_datetime(d["date"])


def period_to_time(p: int) -> str:
    """EFA-day SP → wall-clock start time. EFA SP 1 = 23:00 of (date-1)."""
    base = 23 * 60          # minutes from 00:00 to 23:00
    mins = (base + (p - 1) * 30) % (24 * 60)
    return f"{mins // 60:02d}:{mins % 60:02d}"


def efa_fetch_predictions(predictions_df, efa_date, value_col):
    """Pull a 48-length array of model predictions for the EFA day ending at
    efa_date. Predictions are stored calendar-keyed (date, period); we
    remap SP 47-48 of (efa_date-1) and SP 1-46 of efa_date."""
    out = np.full(48, np.nan, dtype=float)
    prev = efa_date - pd.Timedelta(days=1)
    sub_prev = predictions_df[predictions_df["date"] == prev]
    if not sub_prev.empty:
        m_prev = dict(zip(sub_prev["period"].astype(int), sub_prev[value_col].astype(float)))
        for j, sp in enumerate([47, 48]):
            if sp in m_prev: out[j] = m_prev[sp]
    sub_curr = predictions_df[predictions_df["date"] == efa_date]
    if not sub_curr.empty:
        m_curr = dict(zip(sub_curr["period"].astype(int), sub_curr[value_col].astype(float)))
        for j in range(46):
            sp = j + 1
            if sp in m_curr: out[j + 2] = m_curr[sp]
    return out


def pick_target_date():
    """Latest day with full 48-SP dispatch AND full Model A coverage.

    Used for the 'today/tomorrow' dispatch + ancillary charts. In live mode
    this is typically the most-recent LP iteration (often today or tomorrow)
    — the LP iterates one day past the last settled BMRS day so the chart
    is forward-looking, not 3-day-lagged. We do NOT filter on dispatch
    activity here: an operator needs to see the LP's recommendation even
    if the LP recommended minimal dispatch (e.g. a flat-price day where
    arbitrage is unprofitable)."""
    d_sp = lp_disp.groupby("date")["period"].nunique()
    a_sp = model_a.groupby("date")["period"].nunique()
    full = sorted(set(d_sp[d_sp == 48].index) & set(a_sp[a_sp == 48].index))
    return full[-1] if full else lp_disp["date"].max()


def pick_latest_day():
    """Latest day with ANY LP revenue row. Used for historical aggregates
    (YTD, monthly chart, daily revenue chart) so they always extend to
    the most recent solved day regardless of dispatch shape."""
    return lp_rev["date"].max()


def parse_eval_metrics():
    """Parse the *_eval.txt files into structured headline metrics for the
    dashboard. Falls back to None values if a file is missing."""
    import re
    def _read(path: Path) -> str:
        try: return path.read_text(encoding="utf-8")
        except FileNotFoundError: return ""

    def _grab(text: str, pattern: str):
        m = re.search(pattern, text, flags=re.MULTILINE)
        return float(m.group(1)) if m else None

    def _per_year(text: str, year: int, pattern_tpl: str):
        """Extract a per-year metric using a template like 'MAE {0:.2f}'."""
        line_re = rf"{year}:\s*({pattern_tpl})"
        m = re.search(line_re, text)
        return float(m.group(2)) if m else None  # group(2) inside the float pattern

    a = _read(PROC / "model_a_ensemble_eval.txt")
    c = _read(PROC / "model_c_eval.txt")
    d = _read(PROC / "model_d_eval.txt")

    # Model D has no overall MAE — compute mean across the 6 products
    d_per_prod = re.findall(r"MAE\s+([\d.]+)\s+N=", d)
    d_overall = round(sum(map(float, d_per_prod)) / len(d_per_prod), 3) if d_per_prod else None

    # Latest-year MAE (Model A ensemble per-year line for current_year)
    current_year = datetime.now(timezone.utc).year
    a_curyear = _grab(a, rf"{current_year}:\s+LEAR=[\d.]+\s+ENSEMBLE=([\d.]+)")
    d_curyear = _grab(d, rf"{current_year}:\s+MAE\s+([\d.]+)")

    # Model B: compute MAE DIRECTLY from the live parquet (same pattern as
    # Model C below). The eval.txt sidecar is rewritten by retrain_model_b.py
    # but can lag the parquet when the quantile retrain finishes without
    # re-emitting eval.txt, leaving the dashboard pinned to a stale headline
    # MAE that predates the Phase 2C quantile rollout.
    b_overall_mae = None
    b_curyear_mae = None
    try:
        from sklearn.metrics import mean_absolute_error
        mb_pq = pd.read_parquet(PROC / "model_b_predictions.parquet")
        mb_pq = mb_pq.dropna(subset=["actual_sbp", "predicted_sbp"])
        if len(mb_pq):
            b_overall_mae = round(
                float(mean_absolute_error(mb_pq["actual_sbp"], mb_pq["predicted_sbp"])), 2
            )
        cy_mb = mb_pq[mb_pq.index.year == current_year]
        if len(cy_mb):
            b_curyear_mae = round(
                float(mean_absolute_error(cy_mb["actual_sbp"], cy_mb["predicted_sbp"])), 2
            )
    except Exception as ex:
        print(f"WARN: Model B metric recompute failed: {ex}")

    # Model C: compute metrics DIRECTLY from the live parquet rather than
    # parsing the eval.txt sidecar. The sidecar is only kept on the GitHub
    # Actions runner (not synced to Supabase), so the local copy can drift
    # behind the actual parquet. Recompute on the fly so the dashboard always
    # reflects the model currently in production.
    c_overall_auc_roc = None
    c_overall_auc_pr  = None
    c_overall_f1      = None
    c_curyear_auc_roc = None
    c_curyear_f1      = None
    try:
        from sklearn.metrics import roc_auc_score, average_precision_score, f1_score
        mc_pq = pd.read_parquet(PROC / "model_c_predictions.parquet")
        mc_pq = mc_pq.dropna(subset=["actual", "prob", "pred"])
        if len(mc_pq) and mc_pq["actual"].nunique() > 1:
            c_overall_auc_roc = round(float(roc_auc_score(mc_pq["actual"], mc_pq["prob"])), 4)
            c_overall_auc_pr  = round(float(average_precision_score(mc_pq["actual"], mc_pq["prob"])), 4)
            c_overall_f1      = round(float(f1_score(mc_pq["actual"], mc_pq["pred"])), 4)
        # Current-year slice
        cy_mc = mc_pq[mc_pq.index.year == current_year]
        if len(cy_mc) and cy_mc["actual"].nunique() > 1:
            c_curyear_auc_roc = round(float(roc_auc_score(cy_mc["actual"], cy_mc["prob"])), 4)
            c_curyear_f1      = round(float(f1_score(cy_mc["actual"], cy_mc["pred"])), 4)
    except Exception as ex:
        print(f"WARN: Model C metric recompute failed: {ex}")

    return {
        "model_a_ensemble_mae_gbp_mwh": _grab(a, r"Overall MAE\s+\(ENSEMBLE\):\s+([\d.]+)"),
        "model_a_lear_mae_gbp_mwh":     _grab(a, r"Overall MAE\s+\(LEAR\):\s+([\d.]+)"),
        "model_b_sbp_mae_gbp_mwh":      b_overall_mae,
        # Model C metrics — recomputed from live parquet (not parsed from
        # eval.txt) so they always reflect the current model.
        "model_c_spike_f1":             c_overall_f1,
        "model_c_spike_auc_roc":        c_overall_auc_roc,
        "model_c_spike_auc_pr":         c_overall_auc_pr,
        "model_d_anc_mae_gbp_mw_h":     d_overall,
        "current_year": current_year,
        "current_year_metrics": {
            "model_a_ensemble_mae":   a_curyear,
            "model_b_sbp_mae":        b_curyear_mae,
            # Keep F1 for dissertation continuity; add AUC-ROC as the headline
            # since F1 collapses under low base rates (Model C predicts SBP > £200
            # which is now <0.5% of SPs).
            "model_c_spike_f1":       c_curyear_f1,
            "model_c_spike_auc_roc":  c_curyear_auc_roc,
            "model_d_anc_mae":        d_curyear,
        },
    }


def compute_naive_da_baseline(latest_day):
    """Naive DA-only arbitrage benchmark: buy lowest 4 h, sell highest 4 h,
    50 MW, RTE 0.88 round-trip, £5/MWh degradation. Annualised per MW.
    The lower-bound 'anyone with a battery and a forecast can do this' line.
    Computed across the LP test window (Jan 2022 onwards)."""
    master = pd.read_parquet(PROC / "master.parquet", engine="fastparquet")
    if "settlement_date" not in master.columns:
        return None
    master["date"] = pd.to_datetime(master["settlement_date"])
    da_wide = master.pivot_table(index="date", columns="settlement_period", values="da_price")

    P_MAX, ETA = 50.0, 0.94
    DEGRAD = 5.0

    daily_naive = []
    for d, row in da_wide.iterrows():
        prices = row.dropna()
        if len(prices) < 24:  # half-day missing → skip
            continue
        sorted_p = prices.sort_values()
        low_avg  = sorted_p.head(8).mean()    # 8 SPs = 4 h
        high_avg = sorted_p.tail(8).mean()
        # 50 MW × 4 h = 200 MWh charged, RTE^2 × 200 = 176.7 MWh discharged
        # (RTE is one-way 0.94 in our LP, so round-trip 0.94*0.94 ≈ 0.8836)
        revenue = P_MAX * ETA * ETA * high_avg * 4
        cost    = P_MAX * low_avg * 4
        degrad  = (200 + 176.7) * DEGRAD
        daily_naive.append(revenue - cost - degrad)

    if not daily_naive:
        return None
    avg_daily = sum(daily_naive) / len(daily_naive)
    return round((avg_daily * 365) / P_MAX, 0)   # £/MW/yr annualised


def main():
    target     = pick_target_date()
    latest_day = pick_latest_day()
    print(f"Target date (dispatch chart): {target.date()}")
    print(f"Latest day (historical aggregates): {latest_day.date()}")

    # ---- Carry-in SoC: end-of-day SoC from the day BEFORE `target` ----
    # The LP rolls SoC forward across days (soc_state = result["soc_end"]).
    # The dispatch parquet preserves this — we read SP 48 of (target - 1) day.
    # Falls back to 50% only if the prior day isn't in the parquet (boot day).
    E_MAX_MWH = 100.0
    carry_in_pct = 50.0
    prev_day = target - pd.Timedelta(days=1)
    prev_sp48 = lp_disp[(lp_disp["date"] == prev_day) & (lp_disp["period"] == 48)]
    if not prev_sp48.empty and "soc_mwh" in lp_disp.columns:
        soc_mwh = float(prev_sp48["soc_mwh"].iloc[0])
        carry_in_pct = round(soc_mwh / E_MAX_MWH * 100, 1)
        print(f"Carry-in SoC: {carry_in_pct}% (end of {prev_day.date()})")
    else:
        print(f"Carry-in SoC: defaulting to 50% (no prior-day SP48 row)")

    # ---- Today's dispatch (48 periods) ----
    # SoC comes straight from the LP's end-of-period trajectory (in MWh) and
    # is converted to % of E_max. If the parquet was produced by an older
    # LP run that didn't persist soc_mwh, fall back to a (lossy)
    # reconstruction so the dashboard still renders something sane.
    E_MAX = 100.0
    today_disp = lp_disp[lp_disp["date"] == target].sort_values("period")
    has_soc = "soc_mwh" in today_disp.columns
    dispatch_today = []
    if has_soc:
        for _, r in today_disp.iterrows():
            dispatch_today.append({
                "period":    int(r["period"]),
                "time":      period_to_time(int(r["period"])),
                "pd_mw":     round(float(r["pd_mw"]), 1),
                "pc_mw":     round(float(r["pc_mw"]), 1),
                "net_mw":    round(float(r["net_mw"]), 1),
                "da_pos_mw": round(float(r["da_pos_mw"]), 1),
                "soc_pct":   round(float(r["soc_mwh"]) / E_MAX * 100, 1),
            })
    else:
        # Fallback: efficiency-aware reconstruction (ETA = 0.94 one-way)
        ETA = 0.94
        soc_mwh = 0.5 * E_MAX
        for _, r in today_disp.iterrows():
            soc_mwh += ETA * float(r["pc_mw"]) * 0.5 - float(r["pd_mw"]) * 0.5 / ETA
            dispatch_today.append({
                "period":    int(r["period"]),
                "time":      period_to_time(int(r["period"])),
                "pd_mw":     round(float(r["pd_mw"]), 1),
                "pc_mw":     round(float(r["pc_mw"]), 1),
                "net_mw":    round(float(r["net_mw"]), 1),
                "da_pos_mw": round(float(r["da_pos_mw"]), 1),
                "soc_pct":   round(soc_mwh / E_MAX * 100, 1),
            })

    # ---- DA forecast for the EFA day ----
    # Model A's predictions live on the calendar grid; remap into the
    # EFA-SP order (SP 1 = 23:00 of target-1).
    da_efa = efa_fetch_predictions(model_a, target, "predicted")
    forecast_da_today = [
        {"period": p + 1, "time": period_to_time(p + 1),
         "price": round(float(v), 1) if not np.isnan(v) else 0.0}
        for p, v in enumerate(da_efa)
    ]

    # ---- Ancillary bids today (capacity-aware: best HIGH + best LOW per block) ----
    # Windows align to NESO's EFA-day boundary (23:00 → 23:00).
    block_windows = {1:"23:00-03:00", 2:"03:00-07:00", 3:"07:00-11:00",
                     4:"11:00-15:00", 5:"15:00-19:00", 6:"19:00-23:00"}
    product_meta = [
        ("dch","DC-H","Dynamic Containment High","high"),
        ("dcl","DC-L","Dynamic Containment Low","low"),
        ("dmh","DM-H","Dynamic Moderation High","high"),
        ("dml","DM-L","Dynamic Moderation Low","low"),
        ("drh","DR-H","Dynamic Regulation High","high"),
        ("drl","DR-L","Dynamic Regulation Low","low"),
        ("ffr","FFR","Firm Frequency Response (legacy)","high"),
    ]
    # Read predicted prices from Model D for the target day
    md_today = model_d[model_d["date"] == target]
    price_map = {}
    for _, r in md_today.iterrows():
        price_map[(r["product"], int(r["efa_block"]))] = float(r["predicted"])
    # Also get the LP's actual commitments per block from lp_anc
    lp_today_anc = lp_anc[lp_anc["date"] == target]
    commit_map = {}
    for _, r in lp_today_anc.iterrows():
        commit_map[(r["product"], int(r["efa_block"]))] = float(r["mw"])

    ancillary_bids_today = []
    for key, prod, name, direction in product_meta:
        blocks = []
        for b in range(1, 7):
            price = price_map.get((key, b), 0.0)
            mw = round(commit_map.get((key, b), 0), 0)
            blocks.append({"block": b, "window": block_windows[b],
                          "mw": int(mw), "price": round(price, 2)})
        ancillary_bids_today.append({"product": prod, "name": name,
                                      "direction": direction, "blocks": blocks})

    # ---- KPIs (calendar YTD, annualised per-MW) ----
    # All historical aggregates anchor on `latest_day` (yesterday in live mode),
    # NOT on `target` (which is the demo-day pick — may differ if yesterday has
    # incomplete SPs).
    year_start = pd.Timestamp(year=latest_day.year, month=1, day=1)
    ytd_lp = lp_rev[(lp_rev["date"] >= year_start) & (lp_rev["date"] <= latest_day)]
    ytd_pf = pf_rev[(pf_rev["date"] >= year_start) & (pf_rev["date"] <= latest_day)]
    days_so_far = max((latest_day - year_start).days + 1, 1)
    ytd_gross = float(ytd_lp["total"].sum())
    ytd_pf_total = float(ytd_pf["total"].sum())
    ytd_pct = ytd_gross / ytd_pf_total * 100 if ytd_pf_total > 0 else 0
    ytd_per_mw_annualised = (ytd_gross / 50.0) * (365.0 / days_so_far)

    # ---- Rolling 12-month (trailing) — more honest than YTD-annualised ----
    # YTD-annualised extrapolates whichever partial-year window we have (heavily
    # weighted to winter in early-year, biased optimistic if winter was strong).
    # Trailing 12m captures a full seasonal cycle and is what industry benchmarks
    # (Modo Energy £/MW/yr) are stated on.
    rolling_start = latest_day - pd.Timedelta(days=364)  # 365-day window inclusive
    r12_lp = lp_rev[(lp_rev["date"] >= rolling_start) & (lp_rev["date"] <= latest_day)]
    r12_pf = pf_rev[(pf_rev["date"] >= rolling_start) & (pf_rev["date"] <= latest_day)]
    r12_days     = max((latest_day - rolling_start).days + 1, 1)
    r12_gross    = float(r12_lp["total"].sum())
    r12_pf_total = float(r12_pf["total"].sum())
    r12_pct_pf   = r12_gross / r12_pf_total * 100 if r12_pf_total > 0 else 0
    r12_per_mw   = r12_gross / 50.0  # £/MW over 365 days = £/MW/yr directly (no annualisation needed)
    # If we have less than a full year of data (e.g. very early cutover),
    # scale up to a full-year projection so the field is always comparable
    # to industry benchmarks.
    if r12_days < 365:
        r12_per_mw *= 365.0 / r12_days
        r12_full_year = False
    else:
        r12_full_year = True

    today_lp_rev = float(lp_rev[lp_rev["date"] == latest_day]["total"].sum())
    today_pf_rev = float(pf_rev[pf_rev["date"] == latest_day]["total"].sum())
    today_pct = today_lp_rev / today_pf_rev * 100 if today_pf_rev > 0 else 0

    # ---- 7-day trailing % PF (smooths out single-day forecast luck) ----
    week_start = latest_day - timedelta(days=6)
    week_lp = float(lp_rev[(lp_rev["date"] >= week_start) & (lp_rev["date"] <= latest_day)]["total"].sum())
    week_pf = float(pf_rev[(pf_rev["date"] >= week_start) & (pf_rev["date"] <= latest_day)]["total"].sum())
    week_pct_pf = week_lp / week_pf * 100 if week_pf > 0 else 0

    # ---- Monthly revenue (last 6 months ENDING AT THE LATEST MONTH, inclusive) ----
    # Current month is partial (MTD); marked so the frontend can label it.
    six_mo_start = (latest_day - pd.DateOffset(months=5)).replace(day=1)
    lp_6mo = lp_rev[(lp_rev["date"] >= six_mo_start) & (lp_rev["date"] <= latest_day)].copy()
    pf_6mo = pf_rev[(pf_rev["date"] >= six_mo_start) & (pf_rev["date"] <= latest_day)].copy()
    lp_6mo["ym"] = lp_6mo["date"].dt.to_period("M").astype(str)
    pf_6mo["ym"] = pf_6mo["date"].dt.to_period("M").astype(str)
    monthly_lp = lp_6mo.groupby("ym")["total"].sum()
    monthly_pf = pf_6mo.groupby("ym")["total"].sum()
    months_in_chart = sorted(set(monthly_lp.index) | set(monthly_pf.index))[-6:]
    current_ym = latest_day.strftime("%Y-%m")
    ytd_revenue_by_month = []
    for m in months_in_chart:
        rev = int(monthly_lp.get(m, 0))
        pf  = int(monthly_pf.get(m, 0))
        entry = {"month": m, "revenue": rev, "pf": pf}
        if m == current_ym:
            entry["partial"] = True
            entry["days_so_far"] = (latest_day.day)
        ytd_revenue_by_month.append(entry)

    # ---- Daily revenue last 30 days ending at latest_day ----
    daily_start = latest_day - timedelta(days=29)
    lp_d_grp = lp_rev[(lp_rev["date"] >= daily_start) & (lp_rev["date"] <= latest_day)] \
                 .groupby("date")["total"].sum()
    pf_d_grp = pf_rev[(pf_rev["date"] >= daily_start) & (pf_rev["date"] <= latest_day)] \
                 .groupby("date")["total"].sum()
    weekday_names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    revenue_daily_last_30d = []
    for offset in range(30):
        d = pd.Timestamp(daily_start.date()) + timedelta(days=offset)
        if d not in lp_d_grp.index or lp_d_grp.get(d, 0) <= 0:
            continue
        realised = int(lp_d_grp.get(d, 0))
        revenue_daily_last_30d.append({
            "date": d.date().isoformat(),
            "weekday": weekday_names[d.dayofweek],
            "realised": realised,
            "pf": int(pf_d_grp.get(d, 0)),
            "da": int(realised * 0.7),
        })

    # ---- Freshness indicators ----
    # last_refreshed: when this snapshot was generated (UTC)
    # last_data_through: latest day in the master parquet (UTC date)
    # last_retrained:  last day a model retrain ran (read from a sentinel
    #                  file written by the retrain scripts; falls back to
    #                  the last walk-forward retrain anchor in the parquet)
    now_utc = datetime.now(timezone.utc)
    last_data_through = pd.Timestamp(latest_day).date().isoformat()
    retrain_sentinel = PROC / "last_retrained.txt"
    if retrain_sentinel.exists():
        last_retrained = retrain_sentinel.read_text(encoding="utf-8").strip()
    else:
        last_retrained = pd.Timestamp(latest_day).to_period("M").start_time.date().isoformat()

    snapshot = {
        "generated_at": now_utc.isoformat().replace("+00:00", "Z"),
        "freshness": {
            "last_refreshed_utc":   now_utc.isoformat().replace("+00:00", "Z"),
            "last_data_through":    last_data_through,
            "dispatch_date":        pd.Timestamp(target).date().isoformat(),
            "last_retrained":       last_retrained,
            "pipeline_mode":        "backtest",  # set to "live" by the daily refresh job
        },
        "asset": {
            "name": "Reference 50 MW / 100 MWh BESS",
            "site": "London LPN HV (worked example)",
            "p_max_mw": 50, "e_max_mwh": 100, "rte": 0.88,
            "soc_carry_in_pct": carry_in_pct,   # actual end-of-previous-day SoC
        },
        "kpis": {
            "today_gross_gbp":      int(today_lp_rev),
            "today_vs_pf_pct":      round(today_pct, 1),
            "week_pct_pf":          round(week_pct_pf, 1),
            "week_gross_gbp":       int(week_lp),
            "ytd_gross_gbp":        int(ytd_gross),
            "ytd_per_mw_gbp":       int(ytd_per_mw_annualised),   # legacy; use rolling_12m_per_mw_gbp instead
            "ytd_pct_pf":           round(ytd_pct, 1),
            # Rolling 12-month (trailing) — the honest per-year figure, seasonally complete
            "rolling_12m_gross_gbp":  int(r12_gross),
            "rolling_12m_per_mw_gbp": int(r12_per_mw),
            "rolling_12m_pct_pf":     round(r12_pct_pf, 1),
            "rolling_12m_full_year":  r12_full_year,
            "rolling_12m_days":       r12_days,
            # Industry-comparable %: rolling 12m ÷ Modo median (£50k/MW/yr)
            "vs_industry_median_pct": int(r12_per_mw / 50_000 * 100),
        },
        "model_metrics": parse_eval_metrics(),
        "benchmarks": {
            # Dynamic — recomputed each weekly run from current master.parquet.
            # Uses trailing 12-month figure (not YTD-annualised) so it's directly
            # comparable to Modo Energy's £/MW/yr industry benchmarks.
            "lp_per_mw_gbp_yr":       int(r12_per_mw),
            "naive_da_per_mw_gbp_yr": compute_naive_da_baseline(latest_day),
            # Static — sourced from Modo Energy 2024 BESS Index public summary.
            # Re-validated annually. See pipeline/benchmarks.md for sources.
            "modo_top_decile_per_mw_gbp_yr": 75000,
            "modo_median_per_mw_gbp_yr":     50000,
            "modo_source": "Modo Energy BESS Index 2024 H2 summary (publicly published)",
            "blackhillock_per_mw_gbp_yr":    142000,
            "blackhillock_source": "Reported in Modo Energy '2024's top performing battery' article",
        },
        "forecast_da_today":    forecast_da_today,
        "dispatch_today":       dispatch_today,
        "ancillary_bids_today": ancillary_bids_today,
        "ytd_revenue_by_month": ytd_revenue_by_month,
        "revenue_daily_last_30d": revenue_daily_last_30d,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    print(f"Wrote {OUT} ({OUT.stat().st_size // 1024} KB)")

    # Print quick summary
    print()
    print("=== Snapshot summary ===")
    print(f"Target date (dispatch chart): {target.date()}")
    print(f"Latest day (aggregates):      {latest_day.date()}")
    print(f"Today revenue (latest_day):   GBP {today_lp_rev:,.0f}   % PF: {today_pct:.1f}%")
    print(f"YTD ({year_start.date()} → {latest_day.date()}):  GBP {ytd_gross:,.0f}  ({days_so_far} days)")
    print(f"YTD per MW (annualised): GBP {ytd_per_mw_annualised:,.0f}")
    print(f"YTD % PF:          {ytd_pct:.2f}%")
    print(f"Dispatch periods today (non-zero): {sum(1 for d in dispatch_today if d['pd_mw'] != 0 or d['pc_mw'] != 0)}/48")
    nz_anc = sum(1 for a in ancillary_bids_today for b in a['blocks'] if b['mw'] > 0)
    print(f"Ancillary commits non-zero blocks: {nz_anc}/42")
    print(f"Monthly chart months: {months_in_chart}")
    print(f"Daily chart days (with dispatch): {len(revenue_daily_last_30d)}")


if __name__ == "__main__":
    main()
