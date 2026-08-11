"""
Clean Pipeline — Stage 4b: Perfect-Foresight Oracle (PF v6, architecture-matched).

Solves the SAME LP as v6_ensemble — same battery, same constraints, same
7-day rolling-horizon MPC pattern — but with **actual prices** as the
forecast inputs. This isolates forecast quality from LP architecture.

CRITICAL methodological invariants:
  - Same horizon: 7 days, commit Day 1
  - Same imbalance cap, cycle cap, SoC band, capacity sharing
  - Same battery (50 MW / 100 MWh / 88% RTE / 2 cycles)
  - Spike premium uses ACTUAL spike indicator (1 if SBP > £200, else 0),
    not a probability
  - Actuals are scored at actual prices (trivial — same prices)

Input:  data/processed/master.parquet
Output: data/processed/pf_v6_dispatch.parquet
        data/processed/pf_v6_revenue.parquet
        data/processed/pf_v6_eval.txt
"""

import sys, time, warnings
from pathlib import Path
import pandas as pd
import numpy as np
import pulp

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")

HERE     = Path(__file__).resolve().parent
PROC     = HERE.parent / "data" / "processed"
MASTER   = PROC / "master.parquet"

import os as _os_pf
_pf_suffix = _os_pf.environ.get("LP_OUTPUT_SUFFIX", "").strip()
_PF_SFX    = f"_{_pf_suffix}" if _pf_suffix else ""
OUT_DISP = PROC / f"pf_v6_dispatch{_PF_SFX}.parquet"
OUT_REV  = PROC / f"pf_v6_revenue{_PF_SFX}.parquet"
OUT_EVAL = PROC / f"pf_v6_eval{_PF_SFX}.txt"

# Same constants as LP v6 — DO NOT diverge
# Overridable via LP_P_MAX / LP_E_MAX for sensitivity analysis.
P_MAX = float(_os_pf.environ.get("LP_P_MAX", "50.0"))
E_MAX = float(_os_pf.environ.get("LP_E_MAX", "100.0"))
SOC_MIN, SOC_MAX = 0.10 * E_MAX, 0.95 * E_MAX
if _os_pf.environ.get("LP_P_MAX") or _os_pf.environ.get("LP_E_MAX"):
    print(f"[SENSITIVITY] PF running with P_MAX={P_MAX} MW, E_MAX={E_MAX} MWh "
          f"(duration {E_MAX/P_MAX:.1f} h)")
ETA = 0.94
# Phase 2 (2026-07-10): degradation cost recalibrated from £5 → £18/MWh.
# See lp_v6_ensemble.py for the derivation. PF must use the identical value
# so LP/PF ratios reflect only forecast quality + auction friction, not
# accounting mismatch.
DEGRAD_COST = float(_os_pf.environ.get("LP_DEGRAD_COST", "18.0"))
MAX_CYCLES = 2.0
DT = 0.5
SPIKE_PREMIUM = 250.0
# Phase 1: same asymmetric + spike-gated + spread-gated imbalance caps as LP v6.
# PF uses ACTUAL prices for "spike_prob" (spike_label_200) and actual spread.
IMB_LIMIT_UP    = 5.0
IMB_LIMIT_DOWN  = 2.0
IMB_SPIKE_PENALTY = 2.0
IMB_MIN_SPREAD    = 15.0
# PF v6 doesn't consume Model B uncertainty width (no quantile output for the
# oracle). The constant is kept here for parity with LP v6 but doesn't gate
# anything in PF's solve. PF retains only the Phase 1 controls.
HORIZON_DAYS = 7

# H-suffix products (DC-H / DM-H / DR-H) respond to OVER-frequency events by
# ABSORBING power (charging) — they share MW headroom with wholesale CHARGE.
# L-suffix products respond to UNDER-frequency events by INJECTING power
# (discharging) — they share MW headroom with wholesale DISCHARGE.
# Constraints below (line 145-146) reflect this directional headroom split.
HIGH = ["dch", "dmh", "drh"]      # H-suffix: CHARGE-side headroom
LOW  = ["dcl", "dml", "drl"]      # L-suffix: DISCHARGE-side headroom
ALL_P = HIGH + LOW

# Phase 1b (2026-07-10): same ancillary acceptance rates as LP v6. PF retains
# these frictions because they reflect *market microstructure* (pay-as-clear
# auction), not information gaps that perfect foresight would resolve. See
# lp_v6_ensemble.py for the source of the calibration.
import os as _os_ab
ANC_ACCEPTANCE_DEFAULTS = {
    "dch": 0.335, "dcl": 0.337, "dmh": 0.216, "dml": 0.162, "drh": 0.186, "drl": 0.264,
}
ANC_ACCEPTANCE = {}
for _p, _default in ANC_ACCEPTANCE_DEFAULTS.items():
    _override = _os_ab.environ.get(f"LP_ANC_ACCEPTANCE_{_p.upper()}", "").strip()
    ANC_ACCEPTANCE[_p] = float(_override) if _override else _default
if _os_ab.environ.get("LP_ANC_ACCEPTANCE_DISABLE", "0").strip() == "1":
    ANC_ACCEPTANCE = {p: 1.0 for p in ANC_ACCEPTANCE_DEFAULTS}

# Phase 3: utilisation-failure penalty
UTIL_PENALTY_DEFAULTS = {"dch": 0.02, "dcl": 0.02, "dmh": 0.03, "dml": 0.03,
                        "drh": 0.05, "drl": 0.05}
UTIL_PENALTY = {}
for _p, _default in UTIL_PENALTY_DEFAULTS.items():
    _override = _os_ab.environ.get(f"LP_UTIL_PENALTY_{_p.upper()}", "").strip()
    UTIL_PENALTY[_p] = float(_override) if _override else _default
ANC_EFFECTIVE_SCALAR = {p: ANC_ACCEPTANCE[p] * (1.0 - UTIL_PENALTY[p]) for p in ANC_ACCEPTANCE_DEFAULTS}
if _os_ab.environ.get("LP_UTIL_PENALTY_DISABLE", "0").strip() == "1":
    ANC_EFFECTIVE_SCALAR = dict(ANC_ACCEPTANCE)

# ---- Path A: empirical acceptance-rate lookup (tier-configurable) ---------
# Same lookup + tier semantics as LP v6 — see lp_v6_ensemble.py for the
# derivation and tier descriptions. PF must use the SAME tier as LP so the
# LP/PF ratio isolates forecast quality.
from pathlib import Path as _Path
_ACC_MODE = _os_ab.environ.get("LP_ACCEPTANCE_MODE", "empirical").lower()
_ACC_TIER = _os_ab.environ.get("LP_ACCEPTANCE_TIER", "top_quartile").lower()
_ACC_TABLE = None

_TIER_FILES = {
    "top_quartile": "acceptance_rates.parquet",
    "top_decile":   "acceptance_rates_top_decile.parquet",
    "market_mean":  "acceptance_rates_market_mean.parquet",
}
_ACC_PATH = _Path(__file__).resolve().parent.parent / "data" / "processed" / _TIER_FILES.get(_ACC_TIER, "acceptance_rates.parquet")

def _load_acceptance_table():
    global _ACC_TABLE
    if _ACC_TABLE is not None:
        return
    if not _ACC_PATH.exists():
        _ACC_TABLE = {}
        return
    df = pd.read_parquet(_ACC_PATH)
    _ACC_TABLE = {
        (str(r["product"]).lower(), int(r["efa_block"]), str(r["month"])):
            float(r["rolling_accept_rate"])
        for _, r in df.iterrows()
        if r["rolling_accept_rate"] == r["rolling_accept_rate"]
    }
    print(f"[PATH A/PF] tier={_ACC_TIER}  loaded {len(_ACC_TABLE):,} rates from {_ACC_PATH.name}")

def anc_effective(product: str, block_1based: int, day) -> float:
    if _ACC_MODE == "scalar":
        return ANC_EFFECTIVE_SCALAR[product]
    _load_acceptance_table()
    p_lo = product.lower()
    month_key = pd.Timestamp(day).strftime("%Y-%m")
    rate = _ACC_TABLE.get((p_lo, int(block_1based), month_key))
    if rate is None:
        return ANC_EFFECTIVE_SCALAR[product]
    return rate * (1.0 - UTIL_PENALTY[product])


# ---- Phase 1c: PF-side bid-price optimisation (2026-07-13) ------------------
# PF has perfect foresight of actual clearing → σ = 0. Under _bid_optimal_coef
# with σ = 0, the LP would pick k = -∞ (bid £0), and P(accept) hits the
# volume-cap ceiling. PF therefore earns VOLUME_CAP × μ_actual per MW per
# hour on ancillary — the physical ceiling under the volume-constrained
# clearing model. This preserves LP/PF-comparability with the LP-side Phase
# 1c coefficient.
import math as _math

VOLUME_CAP = float(_os_ab.environ.get("LP_VOLUME_CAP", "0.70").strip() or "0.70")

def _pf_bid_coef(mu_actual: float, util_pen: float = 0.0) -> float:
    if mu_actual is None or mu_actual != mu_actual or mu_actual <= 0:
        return 0.0
    return VOLUME_CAP * mu_actual * (1.0 - util_pen)

# Phase 4: DA execution slippage — same £/MWh as LP so LP/PF ratio is unbiased.
SLIPPAGE_GBP_MWH = float(_os_ab.environ.get("LP_SLIPPAGE_GBP_MWH", "2.0"))

TEST_START = pd.Timestamp("2022-01-01")
import os as _os
_env_end = _os.environ.get("LIVE_TEST_END", "").strip()
TEST_END   = pd.Timestamp(_env_end) if _env_end else pd.Timestamp("2026-05-31")

t0 = time.time()
def log(m): print(f"[{(time.time()-t0):6.1f}s] {m}", flush=True)


# EFA-day helpers — mirrors lp_v6_ensemble.py
EFA_BLOCK_REP_SP = {1: 1, 2: 7, 3: 15, 4: 23, 5: 31, 6: 39}

def efa_fetch_array(wide_df, efa_date, fill=np.nan):
    """Pull 48-length array for EFA day ending at 23:00 of efa_date."""
    arr = np.full(48, fill, dtype=float)
    prev = efa_date - pd.Timedelta(days=1)
    if prev in wide_df.index:
        row_prev = wide_df.loc[prev]
        for j, sp in enumerate([47, 48]):
            if sp in row_prev.index and pd.notna(row_prev[sp]):
                arr[j] = row_prev[sp]
    if efa_date in wide_df.index:
        row_curr = wide_df.loc[efa_date]
        for j in range(46):
            sp = j + 1
            if sp in row_curr.index and pd.notna(row_curr[sp]):
                arr[j + 2] = row_curr[sp]
    return arr


def build_efa_anc_wide(master_df, products):
    out = {}
    rep_sp_to_blk = {v: k for k, v in EFA_BLOCK_REP_SP.items()}
    for p in products:
        col = f"anc_{p}_price"
        if col not in master_df.columns:
            out[p] = pd.DataFrame()
            continue
        sub = master_df[master_df["sp"].isin(EFA_BLOCK_REP_SP.values())][["date", "sp", col]].copy()
        wide = sub.pivot_table(index="date", columns="sp", values=col, aggfunc="mean")
        wide = wide.rename(columns=rep_sp_to_blk)
        wide = wide[[c for c in [1, 2, 3, 4, 5, 6] if c in wide.columns]]
        out[p] = wide
    return out


def solve_horizon(d0, horizon_data, soc_init):
    H = HORIZON_DAYS * 48
    prob = pulp.LpProblem(f"pf_{d0}", pulp.LpMaximize)

    pd_v  = [pulp.LpVariable(f"pd_{i}",  lowBound=0, upBound=P_MAX) for i in range(H)]
    pc_v  = [pulp.LpVariable(f"pc_{i}",  lowBound=0, upBound=P_MAX) for i in range(H)]
    da_v  = [pulp.LpVariable(f"da_{i}",  lowBound=-P_MAX, upBound=P_MAX) for i in range(H)]
    soc_v = [pulp.LpVariable(f"soc_{i}", lowBound=SOC_MIN, upBound=SOC_MAX) for i in range(H + 1)]
    anc_v = {(p, b): pulp.LpVariable(f"anc_{p}_b{b}", lowBound=0, upBound=P_MAX)
             for p in ALL_P for b in range(HORIZON_DAYS * 6)}

    obj_terms = []
    for i in range(H):
        da_p  = horizon_data["da_act"][i]
        sbp_p = horizon_data["sbp_act"][i]
        spk_p = horizon_data["spike_act"][i]
        da_eff = da_p + SPIKE_PREMIUM * spk_p
        obj_terms.append(da_eff * da_v[i] * DT)
        obj_terms.append(sbp_p * (pd_v[i] - pc_v[i] - da_v[i]) * DT)
        obj_terms.append(-DEGRAD_COST * (pd_v[i] + pc_v[i]) * DT)
    for (p, b), v in anc_v.items():
        # Phase 1c: PF earns VOLUME_CAP × actual_clearing per MW per hour on
        # ancillary. Perfect foresight of prices → σ = 0 → aggressive bidding
        # → capped only by physical clearing volume constraint.
        mu_actual = horizon_data["anc_act"][(p, b)]
        coef = _pf_bid_coef(mu_actual, util_pen=UTIL_PENALTY[p])
        obj_terms.append(coef * v * 4.0)
    # Phase 4: DA execution slippage.
    for i in range(H):
        obj_terms.append(-SLIPPAGE_GBP_MWH * (pd_v[i] + pc_v[i]) * DT)
    prob += pulp.lpSum(obj_terms)

    prob += soc_v[0] == soc_init
    for i in range(H):
        prob += soc_v[i + 1] == soc_v[i] + ETA * pc_v[i] * DT - pd_v[i] * DT / ETA
    for i in range(H):
        b_h = i // 8
        # H-suffix ancillary shares MW with wholesale CHARGE; L-suffix with DISCHARGE.
        # (Prior versions had this inverted — fixed 2026-07-09 for LP+PF parity.)
        prob += pc_v[i] + pulp.lpSum(anc_v[(p, b_h)] for p in HIGH) <= P_MAX
        prob += pd_v[i] + pulp.lpSum(anc_v[(p, b_h)] for p in LOW ) <= P_MAX
        # Phase 1 risk controls applied to PF as well (architecture-matched).
        sbp_p  = horizon_data["sbp_act"][i]
        da_p   = horizon_data["da_act"][i]
        spk_p  = horizon_data["spike_act"][i]
        spike_mult = max(0.0, 1.0 - spk_p * IMB_SPIKE_PENALTY)
        spread_mult = 1.0 if abs(da_p - sbp_p) >= IMB_MIN_SPREAD else 0.0
        gate = spike_mult * spread_mult
        eff_up   = IMB_LIMIT_UP   * gate
        eff_down = IMB_LIMIT_DOWN * gate
        prob += pd_v[i] - pc_v[i] - da_v[i] <=  eff_up
        prob += pd_v[i] - pc_v[i] - da_v[i] >= -eff_down
    for day in range(HORIZON_DAYS):
        i0, i1 = day * 48, (day + 1) * 48
        prob += pulp.lpSum(pd_v[i] * DT for i in range(i0, i1)) <= MAX_CYCLES * E_MAX

    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=120)
    status = prob.solve(solver)
    if pulp.LpStatus[status] not in ("Optimal", "Optimal Tolerance"):
        return None

    return {
        "pd": [pd_v[i].value() or 0.0 for i in range(48)],
        "pc": [pc_v[i].value() or 0.0 for i in range(48)],
        "da": [da_v[i].value() or 0.0 for i in range(48)],
        "soc": [soc_v[i + 1].value() if soc_v[i + 1].value() is not None else soc_init
                for i in range(48)],
        "soc_end": soc_v[48].value() or soc_init,
        "anc_d1": {p: [anc_v[(p, b)].value() or 0.0 for b in range(6)] for p in ALL_P},
    }


def main():
    log("=" * 78); log("PF v6 oracle — architecture-matched perfect foresight"); log("=" * 78)
    df = pd.read_parquet(MASTER, engine="fastparquet")
    df["date"] = pd.to_datetime(df["settlement_date"])
    df["sp"]   = df["settlement_period"]
    da_w  = df.pivot_table(index="date", columns="settlement_period", values="da_price")
    sbp_w = df.pivot_table(index="date", columns="settlement_period", values="sbp")
    sp_w  = df.pivot_table(index="date", columns="settlement_period", values="spike_label_200")
    # EFA-aligned ancillary actuals — per product, indexed by EFA-end-date,
    # columns = EFA block 1..6.
    anc_d = build_efa_anc_wide(df, ALL_P)

    test_dates = pd.date_range(TEST_START, TEST_END, freq="D")
    soc_state  = 0.5 * E_MAX
    dispatch_rows, revenue_rows = [], []
    n_skip = 0

    for di, d in enumerate(test_dates):
        try:
            da_a, sbp_a, spk_a, anc_a = [], [], [], {}
            for off in range(HORIZON_DAYS):
                dh = d + pd.Timedelta(days=off)
                # EFA-day-aligned fetches
                arr = efa_fetch_array(da_w, dh, fill=np.nan)
                if np.all(np.isnan(arr)):
                    arr = np.full(48, 50.0)
                else:
                    mean_val = np.nanmean(arr)
                    arr = np.where(np.isnan(arr), mean_val, arr)
                da_a.extend(arr)
                sarr = efa_fetch_array(sbp_w, dh, fill=50.0)
                sarr = np.where(np.isnan(sarr), 50.0, sarr)
                sbp_a.extend(sarr)
                parr = efa_fetch_array(sp_w, dh, fill=0.0)
                parr = np.where(np.isnan(parr), 0.0, parr)
                spk_a.extend(parr)
            for p in ALL_P:
                vals = []
                for off in range(HORIZON_DAYS):
                    dh = d + pd.Timedelta(days=off)
                    if dh in anc_d[p].index:
                        v = anc_d[p].loc[dh].reindex(range(1, 7)).fillna(0).values
                    else:
                        v = np.zeros(6)
                    vals.extend(v)
                for bi, v in enumerate(vals):
                    anc_a[(p, bi)] = float(v)

            res = solve_horizon(d, {"da_act": da_a, "sbp_act": sbp_a,
                                    "spike_act": spk_a, "anc_act": anc_a}, soc_state)
            if res is None:
                n_skip += 1
                continue

            # EFA-day actuals for revenue scoring
            da_act_d  = efa_fetch_array(da_w,  d, fill=np.nan)
            sbp_act_d = efa_fetch_array(sbp_w, d, fill=np.nan)

            da_rev = imb_rev = deg = slippage = 0.0
            for i in range(48):
                pdi, pci, dai = res["pd"][i], res["pc"][i], res["da"][i]
                if not np.isnan(da_act_d[i]):
                    da_rev  += da_act_d[i]  * dai * DT
                if not np.isnan(sbp_act_d[i]):
                    imb_rev += sbp_act_d[i] * (pdi - pci - dai) * DT
                deg += DEGRAD_COST * (pdi + pci) * DT
                slippage += SLIPPAGE_GBP_MWH * (pdi + pci) * DT
                dispatch_rows.append({"date": d, "period": i + 1,
                                      "pd_mw": pdi, "pc_mw": pci,
                                      "net_mw": pdi - pci, "da_pos_mw": dai,
                                      "soc_mwh": res["soc"][i]})
            anc_rev = 0.0
            for p in ALL_P:
                blk_act = anc_d[p].loc[d].reindex(range(1, 7)).fillna(0).values if d in anc_d[p].index else np.zeros(6)
                for b in range(6):
                    # Phase 1c: PF earns VOLUME_CAP × actual_clearing per MW/h.
                    coef = _pf_bid_coef(float(blk_act[b]), util_pen=UTIL_PENALTY[p])
                    anc_rev += res["anc_d1"][p][b] * 4.0 * coef
            total = da_rev + imb_rev + anc_rev - deg - slippage
            revenue_rows.append({"date": d, "da": da_rev, "imb": imb_rev,
                                 "anc": anc_rev, "deg": deg,
                                 "slippage": slippage, "total": total})
            soc_state = res["soc_end"]
            if (di + 1) % 50 == 0:
                log(f"  day {di+1}/{len(test_dates)} ({d.date()}): solved={len(revenue_rows):,} skipped={n_skip:,}")
        except Exception as ex:
            log(f"  WARN day {d.date()}: {ex}")
            n_skip += 1

    pd.DataFrame(dispatch_rows).to_parquet(OUT_DISP)
    rev_df = pd.DataFrame(revenue_rows)
    rev_df.to_parquet(OUT_REV)
    log(f"Saved {OUT_DISP}, {OUT_REV}.  Days solved: {len(rev_df):,}  skipped: {n_skip:,}")

    if len(rev_df) > 0:
        with open(OUT_EVAL, "w", encoding="utf-8") as fout:
            fout.write("=" * 70 + "\n")
            fout.write("PF v6 — architecture-matched oracle\n")
            fout.write("=" * 70 + "\n")
            fout.write(f"Days solved:   {len(rev_df):,}\n")
            fout.write(f"Total revenue: GBP {rev_df['total'].sum():,.0f}\n")
            fout.write(f"  DA:          GBP {rev_df['da'].sum():,.0f}\n")
            fout.write(f"  Imbalance:   GBP {rev_df['imb'].sum():,.0f}\n")
            fout.write(f"  Ancillary:   GBP {rev_df['anc'].sum():,.0f}\n")
            fout.write(f"  Degradation: GBP {rev_df['deg'].sum():,.0f}\n\n")
            rev_df["year"] = pd.to_datetime(rev_df["date"]).dt.year
            for yr in sorted(rev_df["year"].unique()):
                g = rev_df[rev_df["year"] == yr]
                fout.write(f"  {yr}: days={len(g):3d}  total=GBP {g['total'].sum():>12,.0f}\n")
        log(f"Eval: {OUT_EVAL}")


if __name__ == "__main__":
    main()
