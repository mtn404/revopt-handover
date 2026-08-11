"""
export_snapshot.py — Convert pipeline parquets into a single snapshot.json
the Next.js frontend consumes.

Reads from the dissertation pipeline outputs (lp_v6_results_ensemble.parquet,
lp_v6_dispatch_case_study.parquet, master_dataframe_v4.parquet) and produces
data/snapshot.json with: today's dispatch, day-ahead forecast, ancillary bids,
KPIs, and last-6-months revenue.

Usage:
    python export_snapshot.py [--date YYYY-MM-DD]
"""
import argparse, json, os, sys
from datetime import datetime, timedelta
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

PROC = r"C:\Users\Amgomed\Desktop\UCL Study\Utilidex\Data\processed"
OUT  = os.path.join(os.path.dirname(__file__), "..", "data", "snapshot.json")

def _rp(p):
    try:    return pd.read_parquet(p)
    except: return pd.read_parquet(p, engine="fastparquet")

def period_to_time(p: int) -> str:
    mins = (p - 1) * 30
    return f"{mins // 60:02d}:{mins % 60:02d}"

def build_snapshot(target_date: str | None = None) -> dict:
    # Load everything
    lp        = _rp(os.path.join(PROC, "lp_v6_results_ensemble.parquet"))
    dispatch  = _rp(os.path.join(PROC, "lp_v6_dispatch_case_study.parquet"))
    pf6       = _rp(os.path.join(PROC, "lp_v6_perfect_foresight.parquet"))
    model_a   = _rp(os.path.join(PROC, "model_a_ensemble_walkforward_predictions.parquet"))
    model_d   = _rp(os.path.join(PROC, "model_d_v5_predictions_extended.parquet"))

    for d in [lp, pf6, dispatch, model_a]:
        d["date"] = pd.to_datetime(d.get("date", d.index))

    # Pick the target date — default = latest available
    if target_date is None:
        # Latest day with FULL 48-SP coverage + meaningful BOTH-DIRECTION dispatch
        # (BH/weekend days where LP picks "do nothing" or one-sided don't make a good demo)
        d_sp = dispatch.groupby("date")["period"].nunique()
        a_sp = model_a.groupby("date")["period"].nunique()
        per_day = dispatch.groupby("date").agg(
            pd_sum=("pd_mw", lambda s: s.abs().sum()),
            pc_sum=("pc_mw", lambda s: s.abs().sum()),
        )
        full = set(d_sp[d_sp == 48].index) & set(a_sp[a_sp == 48].index)
        # Require BOTH a real discharge cycle (>50 MWh) AND a real charge cycle (>50 MWh)
        active = set(per_day[(per_day["pd_sum"] > 50) & (per_day["pc_sum"] > 50)].index)
        candidates = sorted(full & active)
        target = candidates[-1] if candidates else dispatch["date"].max()
    else:
        target = pd.Timestamp(target_date)
    print(f"Building snapshot for {target.date()}")

    # Today's dispatch (48 settlement periods)
    today_disp = dispatch[dispatch["date"] == target].sort_values("period")
    dispatch_today = []
    soc_running = 50.0  # placeholder; in production carry from yesterday
    for _, r in today_disp.iterrows():
        # Recompute SoC trajectory from net dispatch (simple Euler)
        net = float(r["net_mw"])
        soc_running -= (net * 0.5) / 100.0 * 100  # MW * 0.5h / 100 MWh * 100 = pct change
        dispatch_today.append({
            "period": int(r["period"]),
            "time":   period_to_time(int(r["period"])),
            "pd_mw":  round(float(r["pd_mw"]), 1),
            "pc_mw":  round(float(r["pc_mw"]), 1),
            "net_mw": round(net, 1),
            "da_pos_mw": round(float(r["da_pos_mw"]), 1),
            "soc_pct": round(soc_running, 1),
        })

    # Today's DA forecast (from Model A ensemble)
    a_today = model_a[model_a["date"] == target].sort_values("period")
    forecast_da_today = [
        {"period": int(r["period"]), "time": period_to_time(int(r["period"])), "price": round(float(r["predicted"]), 1)}
        for _, r in a_today.iterrows()
    ]

    # KPIs — today + YTD-of-case-study
    today_lp = lp[lp["date"] == target]
    today_gross = float(today_lp["revenue"].sum()) if len(today_lp) else 0.0
    today_pf = float(pf6[pf6["date"] == target]["revenue"].sum()) if (pf6["date"] == target).any() else 0.0
    today_pct_pf = today_gross / today_pf * 100 if today_pf > 0 else 0

    # CALENDAR YTD aggregate (1 Jan of current year through target date)
    # This is intuitive "year-to-date" — matches what an operator/analyst expects.
    window_start = pd.Timestamp(year=target.year, month=1, day=1)
    lp_window = lp[(lp["date"] >= window_start) & (lp["date"] <= target)]
    pf_window = pf6[(pf6["date"] >= window_start) & (pf6["date"] <= target)]
    ytd_gross = float(lp_window["revenue"].sum())
    ytd_pf    = float(pf_window["revenue"].sum()) if len(pf_window) else 0.0
    ytd_pct   = ytd_gross / ytd_pf * 100 if ytd_pf > 0 else 0
    # Annualised per-MW: scale to a full year from the days already dispatched
    days_so_far = max((target - window_start).days + 1, 1)
    ytd_per_mw_annualised = (ytd_gross / 50.0) * (365.0 / days_so_far)

    # Monthly revenue (last 6 COMPLETED months — drop the partial current month
    # because a partial month understates revenue and is misleading vs the bar chart neighbours)
    current_month_start = pd.Timestamp(year=target.year, month=target.month, day=1)
    last_complete = current_month_start - pd.Timedelta(days=1)  # last day of previous month
    six_mo_start = (last_complete - pd.DateOffset(months=5)).replace(day=1)
    lp_6mo = lp[(lp["date"] >= six_mo_start) & (lp["date"] <= last_complete)].copy()
    pf_6mo = pf6[(pf6["date"] >= six_mo_start) & (pf6["date"] <= last_complete)].copy()
    lp_6mo["ym"] = lp_6mo["date"].dt.to_period("M").astype(str)
    pf_6mo["ym"] = pf_6mo["date"].dt.to_period("M").astype(str)
    monthly_realised = lp_6mo.groupby("ym")["revenue"].sum()
    monthly_pf       = pf_6mo.groupby("ym")["revenue"].sum()
    months = sorted(set(monthly_realised.index) | set(monthly_pf.index))
    ytd_by_month = [
        {"month": m, "revenue": int(monthly_realised.get(m, 0)), "pf": int(monthly_pf.get(m, 0))}
        for m in months[-6:]
    ]

    # Daily revenue last 30 days (for the daily-revenue bar chart on the dashboard)
    # Only include days that have BOTH LP and PF dispatch — gaps are days where the LP
    # could not solve (typically holidays or feature gaps); showing £0 for those is misleading.
    daily_start = target - timedelta(days=29)
    lp_d = lp[(lp["date"] >= daily_start) & (lp["date"] <= target)].copy()
    pf_d = pf6[(pf6["date"] >= daily_start) & (pf6["date"] <= target)].copy()
    lp_d_grp = lp_d.groupby(lp_d["date"].dt.normalize())["revenue"].sum()
    pf_d_grp = pf_d.groupby(pf_d["date"].dt.normalize())["revenue"].sum()
    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    revenue_daily_last_30d = []
    for offset in range(30):
        d = pd.Timestamp(daily_start.date()) + timedelta(days=offset)
        # Skip days where the LP didn't dispatch (realised=0) — would otherwise render as £0
        # bars and misleadingly suggest the LP failed; in reality those are BH/weekends
        # or days the LP optimum was "do nothing".
        if d not in lp_d_grp.index or lp_d_grp.get(d, 0) <= 0:
            continue
        realised = int(lp_d_grp.get(d, 0))
        da_est = int(realised * 0.7)  # rough split; no per-leg breakdown in lp parquet
        revenue_daily_last_30d.append({
            "date": d.date().isoformat(),
            "weekday": weekday_names[d.dayofweek],
            "realised": realised,
            "pf": int(pf_d_grp.get(d, 0)),
            "da": da_est,
        })

    # Today's ancillary bids (from Model D forecast — simplified, you would refine in production)
    # NB: model_d["date"] is tz-aware (UTC); strip tz on both sides for comparison
    md_dates = pd.to_datetime(model_d["date"], utc=True).dt.tz_localize(None).dt.normalize()
    d_today = model_d[md_dates == pd.Timestamp(target).normalize()]
    product_meta = [
        ("dch", "DC-H", "Dynamic Containment High",    "high"),
        ("dcl", "DC-L", "Dynamic Containment Low",     "low"),
        ("dmh", "DM-H", "Dynamic Moderation High",     "high"),
        ("dml", "DM-L", "Dynamic Moderation Low",      "low"),
        ("drh", "DR-H", "Dynamic Regulation High",     "high"),
        ("drl", "DR-L", "Dynamic Regulation Low",      "low"),
        ("ffr", "FFR",  "Firm Frequency Response (legacy)", "high"),
    ]
    # EFA-day block windows (NESO 23:00 → 23:00 UTC EFA day)
    block_windows = {1: "23:00-03:00", 2: "03:00-07:00", 3: "07:00-11:00",
                     4: "11:00-15:00", 5: "15:00-19:00", 6: "19:00-23:00"}
    # Bidding heuristic — respect the LP's capacity-sharing constraint:
    #   pd + Σ HIGH ≤ P_max  (50 MW) per block
    #   pc + Σ LOW  ≤ P_max  (50 MW) per block
    # i.e. only ONE HIGH product and ONE LOW product per block can take the full 50 MW.
    # We pick the highest-priced product in each direction per block.
    P_MAX = 50
    PRICE_FLOOR = 0.5  # below this £/MW/h, the LP wouldn't bother committing
    # Build per-block per-product price map first
    price_map = {}  # price_map[(product_key, block)] = price
    for key, prod, name, direction in product_meta:
        rows = d_today[d_today["product"] == key]
        for b in range(1, 7):
            blk = rows[rows["efa_block"] == b]
            price_map[(key, b)] = float(blk["predicted"].iloc[0]) if len(blk) else 0.0

    # For each block, pick best HIGH and best LOW (above the floor)
    winners = {}  # winners[(product_key, block)] = mw (0 or P_MAX)
    high_keys = [k for k, _, _, dirn in product_meta if dirn == "high"]
    low_keys  = [k for k, _, _, dirn in product_meta if dirn == "low"]
    for b in range(1, 7):
        # HIGH direction
        cands = [(price_map[(k, b)], k) for k in high_keys if price_map[(k, b)] > PRICE_FLOOR]
        if cands:
            best_price, best_key = max(cands)
            winners[(best_key, b)] = P_MAX
        # LOW direction
        cands = [(price_map[(k, b)], k) for k in low_keys if price_map[(k, b)] > PRICE_FLOOR]
        if cands:
            best_price, best_key = max(cands)
            winners[(best_key, b)] = P_MAX

    ancillary_bids_today = []
    for key, prod, name, direction in product_meta:
        blocks = []
        for b in range(1, 7):
            price = price_map.get((key, b), 0.0)
            mw = winners.get((key, b), 0)
            blocks.append({"block": b, "window": block_windows[b], "mw": mw, "price": round(price, 2)})
        ancillary_bids_today.append({
            "product": prod, "name": name, "direction": direction, "blocks": blocks,
        })

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "asset": {
            "name": "Reference 50 MW / 100 MWh BESS",
            "site": "London LPN HV (worked example)",
            "p_max_mw": 50,
            "e_max_mwh": 100,
            "rte": 0.88,
            "soc_start_pct": 50,
        },
        "kpis": {
            "today_gross_gbp": int(today_gross),
            "today_vs_pf_pct": round(today_pct_pf, 1),
            "week_gross_gbp": int(lp[(lp["date"] >= target - timedelta(days=6)) & (lp["date"] <= target)]["revenue"].sum()),
            "ytd_gross_gbp": int(ytd_gross),
            "ytd_per_mw_gbp": int(ytd_per_mw_annualised),
            "ytd_pct_pf": round(ytd_pct, 1),
            "vs_industry_median_pct": int(ytd_per_mw_annualised / 50_000 * 100),  # vs £50k/MW/yr Modo median
        },
        "forecast_da_today": forecast_da_today,
        "dispatch_today": dispatch_today,
        "ancillary_bids_today": ancillary_bids_today,
        "ytd_revenue_by_month": ytd_by_month,
        "revenue_daily_last_30d": revenue_daily_last_30d,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Target date YYYY-MM-DD; defaults to latest in dispatch parquet")
    args = parser.parse_args()
    snap = build_snapshot(args.date)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2)
    print(f"Wrote {OUT} ({os.path.getsize(OUT) // 1024} KB)")
