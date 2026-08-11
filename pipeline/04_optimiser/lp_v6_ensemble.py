"""
Clean Pipeline — Stage 4a: LP v6 — multi-day rolling-horizon LP with imbalance leg.

Headline LP. Uses ENSEMBLE Model A predictions for DA, Model B for SBP,
Model C for spike-aware effective price, Model D per-product per-block
for ancillary clearings.

Architecture (canonical v6):
  - 7-day rolling MPC horizon (commits only Day 1, discards Days 2-7)
  - Decision variables per half-hour:
      pd[t]      physical discharge MW
      pc[t]      physical charge MW
      da_pos[t]  day-ahead market position (decoupled from physical, capped)
  - Ancillary per (product, EFA block), block index b ∈ {1..6}:
      anc[p, b]  MW committed
  - Constraints:
      Physical:     0 ≤ pd, pc ≤ P_max   (only one direction nonzero per period)
      Capacity:     pd[t] + Σ_HIGH anc[p, block(t)] ≤ P_max
                    pc[t] + Σ_LOW  anc[p, block(t)] ≤ P_max
      SoC:          SOC_MIN ≤ soc[t] ≤ SOC_MAX,  soc[t] = soc[t-1] + ηc·pc·dt - pd·dt/ηd
      Cycles:       Σ pd·dt ≤ MAX_CYCLES × E_max
      Imbalance:    |pd[t] - pc[t] - da_pos[t]| ≤ IMB_LIMIT
  - Objective:
      max Σ_t [ DA_eff[t] · da_pos[t] · dt              (DA leg)
              + SBP_pred[t] · (pd - pc - da_pos)[t] · dt (imbalance leg)
              + Σ_p,b ANC_pred[p, b] · anc[p, b] · 4    (ancillary leg)
              − DEGRAD_COST · (pd[t] + pc[t]) · dt ]
    where DA_eff[t] = DA_pred[t] + SPIKE_PREMIUM · spike_prob[t]
                                                  (effective price uplift on likely-spike periods)

Input:  data/processed/master.parquet
        data/processed/model_a_ensemble_predictions.parquet
        data/processed/model_b_predictions.parquet
        data/processed/model_c_predictions.parquet
        data/processed/model_d_predictions.parquet

Output: data/processed/lp_v6_ensemble_dispatch.parquet  (half-hourly: date, period, pd, pc, da_pos, soc)
        data/processed/lp_v6_ensemble_revenue.parquet   (daily: date, da, imb, anc, deg, total)
        data/processed/lp_v6_ensemble_anc.parquet       (daily per-product per-block commitments)
        data/processed/lp_v6_ensemble_eval.txt
"""

import sys, time, warnings
from pathlib import Path
import pandas as pd
import numpy as np
import pulp

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")

HERE      = Path(__file__).resolve().parent
PROC      = HERE.parent / "data" / "processed"
MASTER    = PROC / "master.parquet"
MODEL_A   = PROC / "model_a_ensemble_predictions.parquet"

# Ablation overrides — let dissertation Stage 2 ablation point to legacy parquets
# without disturbing the production schedule. Empty / unset => use default path.
import os as _os_ab
_b_override = _os_ab.environ.get("LP_MODEL_B_PARQUET", "").strip()
_c_override = _os_ab.environ.get("LP_MODEL_C_PARQUET", "").strip()
MODEL_B   = Path(_b_override) if _b_override else PROC / "model_b_predictions.parquet"
MODEL_C   = Path(_c_override) if _c_override else PROC / "model_c_predictions.parquet"
MODEL_D   = PROC / "model_d_predictions.parquet"

# Ablation output-suffix — when running variant backtests, redirect outputs so
# the production parquets are not overwritten.
_out_suffix = _os_ab.environ.get("LP_OUTPUT_SUFFIX", "").strip()
OUT_SUFFIX = f"_{_out_suffix}" if _out_suffix else ""

OUT_DISP  = PROC / f"lp_v6_ensemble_dispatch{OUT_SUFFIX}.parquet"
OUT_REV   = PROC / f"lp_v6_ensemble_revenue{OUT_SUFFIX}.parquet"
OUT_ANC   = PROC / f"lp_v6_ensemble_anc{OUT_SUFFIX}.parquet"
OUT_EVAL  = PROC / f"lp_v6_ensemble_eval{OUT_SUFFIX}.txt"

# Battery — overridable for sensitivity analysis
# Env vars LP_P_MAX and LP_E_MAX, in MW and MWh respectively.
P_MAX   = float(_os_ab.environ.get("LP_P_MAX", "50.0"))   # MW
E_MAX   = float(_os_ab.environ.get("LP_E_MAX", "100.0"))  # MWh
SOC_MIN = 0.10 * E_MAX
SOC_MAX = 0.95 * E_MAX
if _os_ab.environ.get("LP_P_MAX") or _os_ab.environ.get("LP_E_MAX"):
    print(f"[SENSITIVITY] LP running with P_MAX={P_MAX} MW, E_MAX={E_MAX} MWh "
          f"(duration {E_MAX/P_MAX:.1f} h)")
ETA     = 0.94      # one-way (RTE = 0.94 × 0.94 = 0.8836)
# Phase 2 (2026-07-10): degradation cost recalibrated from a placeholder £5/MWh
# to a citation-backed £18/MWh of energy throughput. Derived from:
#   augmentation capex $250/kWh × EoL capacity loss 25% ÷ warranted throughput
#   ~6,000 equivalent full cycles ≈ £8-10/MWh in cell cost alone,
#   scaled up to ~£15-20/MWh once transformer/inverter warranty, insurance,
#   labour, and grid-charge escalation are included (industry practice,
#   see e.g. Modo Energy operational cost benchmarks 2024, Fluence
#   warranty datasheets, Wärtsilä LCOS reports).
# Overridable via LP_DEGRAD_COST env var for the sensitivity variants at
# £12 (low) and £25 (conservative-high).
DEGRAD_COST   = float(_os_ab.environ.get("LP_DEGRAD_COST", "18.0"))
if _os_ab.environ.get("LP_DEGRAD_COST"):
    print(f"[SENSITIVITY] LP running with DEGRAD_COST=£{DEGRAD_COST}/MWh")
MAX_CYCLES    = 2.0    # per day
DT            = 0.5    # hours per settlement period
SPIKE_PREMIUM = 250.0  # £/MWh applied per spike-probability
# ---- Phase 1 imbalance risk controls ----
# Asymmetric position cap: long (over-deliver) more than short (under-deliver).
# When SBP spikes UP — the dominant failure mode — long imbalance wins, short
# loses. Skewing the limit toward "long-friendly" cuts the tail loss.
IMB_LIMIT_UP    = 5.0    # MW: long-imbalance cap (deliver more than committed)
IMB_LIMIT_DOWN  = 2.0    # MW: short-imbalance cap (deliver less than committed)
# Spike-probability gate: shrink the cap when Model C predicts a spike likely.
# Multiplier applied to BOTH caps: (1 - spike_prob × IMB_SPIKE_PENALTY).
IMB_SPIKE_PENALTY = 2.0  # at spike_prob = 0.5, cap is fully zero
# Minimum predicted (DA − SBP) spread before LP takes any imbalance position.
# Below this threshold the LP forces imb = 0 (gates out marginal bets).
IMB_MIN_SPREAD    = 15.0 # £/MWh

# Ablation override: disable all Phase 1 risk controls.
# When LP_DISABLE_RISK_CONTROLS=1, this reverts to the pre-Phase 1 architecture:
#   - symmetric +/-5 MW imbalance cap (vs asymmetric +5/-2)
#   - no spike-probability gate (multiplier = 1.0)
#   - no minimum-spread gate (threshold = 0)
# Used to compute the v6+P0 baseline in the dissertation Stage 2 ablation.
if _os_ab.environ.get("LP_DISABLE_RISK_CONTROLS", "0").strip() == "1":
    IMB_LIMIT_DOWN    = 5.0   # symmetric (matches pre-Phase 1 v6 ENSEMBLE)
    IMB_SPIKE_PENALTY = 0.0   # spike gate disabled
    IMB_MIN_SPREAD    = 0.0   # spread gate disabled
    print(f"[ABLATION] Phase 1 risk controls DISABLED via LP_DISABLE_RISK_CONTROLS=1")
# ---- Phase 2E uncertainty-aware sizing ----
# Model B quantile width (P95 − P05) signals how confident the SBP forecast
# is. When width is large (e.g. > IMB_UNCERTAINTY_REF), the LP shrinks its
# imbalance position because the directional prediction is unreliable.
# Multiplier: max(0, 1 − width / IMB_UNCERTAINTY_REF) — zero when width ≥ ref.
# Production default: 10,000 (gate effectively disabled) — the sensitivity
# sweep showed capture is invariant within 0.03 pp across thresholds, so we
# ship with the highest-capture variant for the production MVP. Operators
# who want explicit risk-aware sizing can lower this via the
# IMB_UNCERTAINTY_REF env var (see sweep_phase2e.py for the calibration curve).
import os as _os_p2e
IMB_UNCERTAINTY_REF = float(_os_p2e.environ.get("IMB_UNCERTAINTY_REF", "10000.0"))
HORIZON_DAYS  = 7

# Product taxonomy
# H-suffix products (DC-H / DM-H / DR-H) respond to OVER-frequency events by
# ABSORBING power (charging) — they require CHARGE headroom in the battery
# and conflict with simultaneous wholesale charge dispatch.
# L-suffix products (DC-L / DM-L / DR-L) respond to UNDER-frequency events by
# INJECTING power (discharging) — they require DISCHARGE headroom and conflict
# with simultaneous wholesale discharge dispatch.
HIGH = ["dch", "dmh", "drh"]      # H-suffix: require CHARGE-side headroom (pc + HIGH ≤ P_MAX)
LOW  = ["dcl", "dml", "drl"]      # L-suffix: require DISCHARGE-side headroom (pd + LOW ≤ P_MAX)
ALL_P = HIGH + LOW

# ---- Phase 1b: ancillary auction acceptance rates ---------------------------
# Real GB Dynamic Containment / Moderation / Regulation auctions are pay-as-
# clear: assets bidding above the marginal accepted price win nothing. Prior
# LP versions implicitly assumed 100 % bid acceptance, which was the largest
# single source of revenue overstatement identified in the 2026-07-10 audit
# against Gresham House / Modo Energy industry references.
#
# The values below are empirically calibrated acceptance rates per product,
# derived from Modo Energy's H2 2024 BESS Index public summary of GB
# ancillary market clearings (average share of registered auction capacity
# that cleared per block, computed across 2024 H2). They act as multiplicative
# coefficients on ancillary revenue in the LP + PF objective — i.e. the LP is
# expected to win ANC_ACCEPTANCE_RATES[product] × its bid, on average, rather
# than 100 %.
#
# Deliberately conservative: individual operators with sophisticated bidding
# strategies can exceed these rates; this LP therefore lands closer to a
# 'typical top-decile' realistic expectation.
#
# Override any product's rate via env var LP_ANC_ACCEPTANCE_<PRODUCT>=<float>
# for sensitivity analyses (e.g. LP_ANC_ACCEPTANCE_DRL=0.65).
ANC_ACCEPTANCE_DEFAULTS = {
    # Fallback per-product means from the empirical NESO Sell-Orders calibration
    # (weighted by submitted MW across all history 2023-11 → 2026-07).
    # Used ONLY when the (product × block × month) lookup has no data for a
    # given target month — e.g. warm-up period at the start of the backtest
    # before 6 months of lookback history has accumulated.
    # See build_acceptance_rate_table.py for the derivation.
    "dch": 0.335,  # DC-H: 33.5% empirical mean across all blocks
    "dcl": 0.337,  # DC-L: 33.7% (Dynamic Containment products cleanest)
    "dmh": 0.216,  # DM-H: 21.6% (Dynamic Moderation, thinner market)
    "dml": 0.162,  # DM-L: 16.2%
    "drh": 0.186,  # DR-H: 18.6% (Dynamic Regulation over-freq, thin)
    "drl": 0.264,  # DR-L: 26.4% (continuous droop, most competitive)
}
ANC_ACCEPTANCE = {}
for _p, _default in ANC_ACCEPTANCE_DEFAULTS.items():
    _override = _os_ab.environ.get(f"LP_ANC_ACCEPTANCE_{_p.upper()}", "").strip()
    ANC_ACCEPTANCE[_p] = float(_override) if _override else _default
if any(_os_ab.environ.get(f"LP_ANC_ACCEPTANCE_{p.upper()}", "").strip() for p in ANC_ACCEPTANCE_DEFAULTS):
    print(f"[PHASE 1b] ancillary acceptance rates: {ANC_ACCEPTANCE}")
# Ablation: LP_ANC_ACCEPTANCE_DISABLE=1 restores the pre-Phase 1b behaviour
# (100 % acceptance everywhere) so the pre/post economic impact can be
# quantified in the Results chapter.
if _os_ab.environ.get("LP_ANC_ACCEPTANCE_DISABLE", "0").strip() == "1":
    ANC_ACCEPTANCE = {p: 1.0 for p in ANC_ACCEPTANCE_DEFAULTS}
    print(f"[ABLATION] LP_ANC_ACCEPTANCE_DISABLE=1  — reverts to 100% acceptance")

# ---- Phase 3: expected utilisation-failure penalty --------------------------
# Even for blocks the operator wins, when NESO calls the ancillary product
# (frequency event) and the battery cannot deliver the full response (due to
# insufficient SoC / headroom / thermal derate), NESO applies a clawback on
# the availability payment for that block. The tariff varies but is typically
# 15-30 % of the block payment forfeited per delivery failure.
#
# Rather than model per-SP energy availability (a nonlinear constraint), we
# apply the expected utilisation penalty as a multiplicative haircut on top
# of the acceptance rate. Rates below reflect:
#   expected_penalty = utilisation_frequency × failure_conditional × clawback
#
# where utilisation_frequency is how often the product is called (DR ≫ DC)
# and failure_conditional is the probability the battery can't deliver given
# a call. Combined ranges:
#   DC products: ~1-3 % (rare-event, high delivery reliability)
#   DM products: ~2-4 % (moderate frequency)
#   DR products: ~4-6 % (continuous droop, higher delivery risk)
UTIL_PENALTY_DEFAULTS = {
    "dch": 0.02, "dcl": 0.02,   # Dynamic Containment: rare-event products
    "dmh": 0.03, "dml": 0.03,   # Dynamic Moderation: moderate frequency
    "drh": 0.05, "drl": 0.05,   # Dynamic Regulation: continuous droop
}
UTIL_PENALTY = {}
for _p, _default in UTIL_PENALTY_DEFAULTS.items():
    _override = _os_ab.environ.get(f"LP_UTIL_PENALTY_{_p.upper()}", "").strip()
    UTIL_PENALTY[_p] = float(_override) if _override else _default
# Compose: effective coefficient = acceptance × (1 - utilisation_penalty)
# ANC_EFFECTIVE_SCALAR is the fallback used when the empirical lookup misses.
ANC_EFFECTIVE_SCALAR = {p: ANC_ACCEPTANCE[p] * (1.0 - UTIL_PENALTY[p]) for p in ANC_ACCEPTANCE_DEFAULTS}
if _os_ab.environ.get("LP_UTIL_PENALTY_DISABLE", "0").strip() == "1":
    ANC_EFFECTIVE_SCALAR = dict(ANC_ACCEPTANCE)
    print(f"[ABLATION] LP_UTIL_PENALTY_DISABLE=1 — utilisation penalty off")

# ---- Phase 1c: bid-price optimisation (2026-07-13) --------------------------
# Model D outputs (μ, σ) per (product, block, day). The LP's ancillary revenue
# per MW per hour is computed by optimising over 5 discrete bid-price levels
# around the predicted clearing, under a Gaussian tail model with a volume
# cap. This replaces the scalar acceptance-rate coefficient (Path A) with a
# per-block strategic bid decision.
#
# Formulation:
#   bid_price(k) = μ + k·σ         where k ∈ {-2, -1, 0, +1, +2}
#   P(accept|k)  = min(1-Φ(k), VOL_CAP)     Gaussian tail × volume constraint
#   E[C|accept,k]= μ + σ·φ(k)/(1-Φ(k))       Gaussian conditional expectation
#   E[rev|k]     = P(accept|k) × E[C|accept,k]
#
# LP picks the k that maximises E[rev|k] per (product, block, day). Levels
# below k=0 (aggressive) are volume-capped; levels above (conservative) are
# price-capped. The optimal k depends on σ/μ ratio — high-uncertainty blocks
# reward moderate bids, high-confidence blocks reward aggressive bids.
#
# Env overrides:
#   LP_ACCEPTANCE_MODE = bid_optimal (default) | empirical | scalar
#   LP_VOLUME_CAP       = 0.70 (default) — max acceptance from volume constraint
#     Set to 1.0 to disable the volume cap (pure Gaussian, LP will always
#     pick k=-2 and earn ~ μ per MW).

import math as _math

VOLUME_CAP = float(_os_ab.environ.get("LP_VOLUME_CAP", "0.70").strip() or "0.70")

# Gaussian PDF/CDF at integer k in {-2, -1, 0, 1, 2}. Pre-computed.
def _norm_pdf(k: float) -> float:
    return _math.exp(-0.5 * k * k) / _math.sqrt(2.0 * _math.pi)
def _norm_cdf(k: float) -> float:
    return 0.5 * (1.0 + _math.erf(k / _math.sqrt(2.0)))

_LEVELS = (-2, -1, 0, 1, 2)
_PHI   = {k: _norm_pdf(k) for k in _LEVELS}
_CDF_K = {k: _norm_cdf(k) for k in _LEVELS}

def _bid_optimal_coef(mu: float, sigma: float, util_pen: float = 0.0) -> tuple:
    """Return (E[rev per MW per hour], best_k) under Gaussian bid-price
    optimisation with volume-cap acceptance.

    Falls back to μ × VOLUME_CAP × (1-util_pen) when σ ≤ 0 (Model D no
    uncertainty output). This gives the LP the top-quartile empirical
    acceptance behaviour as a safety net.
    """
    if mu is None or mu != mu or mu <= 0:
        return 0.0, 0
    if sigma is None or sigma != sigma or sigma <= 0:
        # No uncertainty info — fall back to volume-cap × μ
        return VOLUME_CAP * mu * (1.0 - util_pen), 0

    best_rev, best_k = 0.0, 0
    for k in _LEVELS:
        surv = 1.0 - _CDF_K[k]                  # P(clearing ≥ bid) under Gaussian
        p_accept = min(surv, VOLUME_CAP)
        if surv <= 1e-9:
            e_clear = mu + sigma * k            # falls back linearly for extreme k
        else:
            e_clear = mu + sigma * _PHI[k] / surv
        e_rev = p_accept * e_clear
        if e_rev > best_rev:
            best_rev, best_k = e_rev, k
    return best_rev * (1.0 - util_pen), best_k


# ---- Path A empirical rates retained as fallback / sensitivity ablation ----
# Loads a rolling-6-month lookup table computed from NESO Sell-Orders history
# (Nov 2023 → present). LP applies rate[product, block-of-day, month] for
# each SP, replacing the scalar Phase 1b coefficient. Leak-free by
# construction (each cell only uses data from the 6 months PRIOR to it).
#
# BIDDER-TIER CALIBRATION:
#   The raw market-mean acceptance rate (~26% overall) reflects the AVERAGE
#   bidder in the market, INCLUDING operators who mispriced their bids. The
#   LP is a strategic forecast-driven bidder that should be evaluated against
#   competitive-bidder peers, not the market mean. Three tiers are shipped:
#
#     top_quartile (DEFAULT) — 11 operators with lifetime acceptance ≥ 75th
#         percentile of the market. Overall rate ~60-75% per product. This
#         represents a well-run trading operation and is the honest LP
#         reference class.
#     top_decile              — 5 operators (VEST ENERGY, GRIDBEYOND,
#         SCOTTISHPOWER RENEWABLES, ECOTRICITY, PEAK GEN). Overall rate
#         ~92-99% per product. Aspirational best-in-class reference.
#     market_mean             — 42 operators (all). Overall rate ~26%.
#         Includes all under-performing bidders; understates the LP.
#
# Override via env var:
#   LP_ACCEPTANCE_TIER = top_quartile (default) | top_decile | market_mean
#   LP_ACCEPTANCE_MODE = empirical (default)    | scalar (Phase 1b ablation)
_ACC_MODE = _os_ab.environ.get("LP_ACCEPTANCE_MODE", "empirical").lower()
_ACC_TIER = _os_ab.environ.get("LP_ACCEPTANCE_TIER", "top_quartile").lower()
_ACC_TABLE = None

_TIER_FILES = {
    "top_quartile": "acceptance_rates.parquet",             # canonical default
    "top_decile":   "acceptance_rates_top_decile.parquet",
    "market_mean":  "acceptance_rates_market_mean.parquet",
}
_ACC_PATH = HERE.parent / "data" / "processed" / _TIER_FILES.get(_ACC_TIER, "acceptance_rates.parquet")

def _load_acceptance_table():
    global _ACC_TABLE
    if _ACC_TABLE is not None:
        return
    if not _ACC_PATH.exists():
        print(f"[PATH A] {_ACC_PATH.name} not found — using scalar fallback")
        _ACC_TABLE = {}
        return
    df = pd.read_parquet(_ACC_PATH)
    _ACC_TABLE = {
        (str(r["product"]).lower(), int(r["efa_block"]), str(r["month"])):
            float(r["rolling_accept_rate"])
        for _, r in df.iterrows()
        if r["rolling_accept_rate"] == r["rolling_accept_rate"]
    }
    print(f"[PATH A] tier={_ACC_TIER}  loaded {len(_ACC_TABLE):,} rates from {_ACC_PATH.name}")

def anc_effective(product: str, block_1based: int, day) -> float:
    """Return effective ancillary coefficient (acceptance × (1 - util_penalty))
    for a given (product, block, day). Uses empirical lookup when available;
    falls back to per-product scalar when the (p, b, month) cell is missing."""
    if _ACC_MODE == "scalar":
        return ANC_EFFECTIVE_SCALAR[product]
    _load_acceptance_table()
    p_lo = product.lower()
    month_key = pd.Timestamp(day).strftime("%Y-%m")
    rate = _ACC_TABLE.get((p_lo, int(block_1based), month_key))
    if rate is None:
        return ANC_EFFECTIVE_SCALAR[product]
    return rate * (1.0 - UTIL_PENALTY[product])

# ---- Phase 4: DA execution slippage -----------------------------------------
# Real DA auction cleared prices differ from BMRS MID by typically £1-3/MWh
# (bid-ask spread, intraday premium in MID). LP is assumed to be a small
# price-taker; slippage is symmetric on charge and discharge sides.
SLIPPAGE_GBP_MWH = float(_os_ab.environ.get("LP_SLIPPAGE_GBP_MWH", "2.0"))
if _os_ab.environ.get("LP_SLIPPAGE_GBP_MWH"):
    print(f"[SENSITIVITY] LP running with SLIPPAGE_GBP_MWH=£{SLIPPAGE_GBP_MWH}/MWh")

TEST_START = pd.Timestamp("2022-01-01")
import os as _os
_env_end = _os.environ.get("LIVE_TEST_END", "").strip()
TEST_END   = pd.Timestamp(_env_end) if _env_end else pd.Timestamp("2026-05-31")

t0 = time.time()
def log(m): print(f"[{(time.time()-t0):6.1f}s] {m}", flush=True)


# ---------------------------------------------------------------------------
# EFA-day semantics
# ---------------------------------------------------------------------------
# An EFA day D ends at 23:00 of calendar date D. It spans 23:00 of (D-1) to
# 23:00 of D, divided into 6 four-hour blocks aligned to NESO's EAC product
# windows:
#     Block 1: EFA SP 1-8    = 23:00 - 03:00
#     Block 2: EFA SP 9-16   =  03:00 - 07:00
#     Block 3: EFA SP 17-24  =  07:00 - 11:00
#     Block 4: EFA SP 25-32  =  11:00 - 15:00
#     Block 5: EFA SP 33-40  =  15:00 - 19:00
#     Block 6: EFA SP 41-48  =  19:00 - 23:00
#
# Mapping EFA SP → calendar (date, sp) within EFA day D:
#     EFA SP 1, 2          → calendar (D-1, SP 47), (D-1, SP 48)
#     EFA SP 3..48         → calendar (D, SP 1..46)
# ---------------------------------------------------------------------------

def efa_fetch_array(wide_df, efa_date, fill=np.nan):
    """Pull a 48-length array for the EFA day ending at 23:00 of efa_date,
    reading from a calendar-date-keyed wide table with SP columns 1..48."""
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


# EFA-block representative SP (within calendar date of EFA day end):
#   Block 1: SP 1 (any of SP 1-6 share the same EFA-1 clearing price)
#   Block 2: SP 7 (SP 7-14)
#   Block 3: SP 15
#   Block 4: SP 23
#   Block 5: SP 31
#   Block 6: SP 39
EFA_BLOCK_REP_SP = {1: 1, 2: 7, 3: 15, 4: 23, 5: 31, 6: 39}


def build_efa_anc_wide(master_df, products):
    """For each product, return a wide table (index=EFA date, cols=EFA block 1-6)
    of EFA-aligned ancillary clearing prices, using the representative SP per block."""
    out = {}
    for p in products:
        col = f"anc_{p}_price"
        if col not in master_df.columns:
            out[p] = pd.DataFrame()
            continue
        rep_sp_to_blk = {v: k for k, v in EFA_BLOCK_REP_SP.items()}
        sub = master_df[master_df["sp"].isin(EFA_BLOCK_REP_SP.values())][["date", "sp", col]].copy()
        wide = sub.pivot_table(index="date", columns="sp", values=col, aggfunc="mean")
        wide = wide.rename(columns=rep_sp_to_blk)
        wide = wide[[c for c in [1, 2, 3, 4, 5, 6] if c in wide.columns]]
        out[p] = wide
    return out


def solve_horizon(d0, horizon_data, soc_init):
    """
    Build and solve LP v6 over the 7-day horizon starting at d0.
    horizon_data: dict with keys 'da_pred', 'sbp_pred', 'spike_prob', 'anc_pred',
                                 'da_actual', 'sbp_actual', 'anc_actual' (each indexed by (day_offset, period))
    Returns: dispatch dict for DAY 1 only (commit pattern).
    """
    H = HORIZON_DAYS * 48          # total half-hour slots
    prob = pulp.LpProblem(f"v6_{d0}", pulp.LpMaximize)

    pd_v   = [pulp.LpVariable(f"pd_{i}",   lowBound=0, upBound=P_MAX) for i in range(H)]
    pc_v   = [pulp.LpVariable(f"pc_{i}",   lowBound=0, upBound=P_MAX) for i in range(H)]
    da_v   = [pulp.LpVariable(f"da_{i}",   lowBound=-P_MAX, upBound=P_MAX) for i in range(H)]
    soc_v  = [pulp.LpVariable(f"soc_{i}",  lowBound=SOC_MIN, upBound=SOC_MAX) for i in range(H + 1)]

    # Ancillary per (product, block, day_in_horizon) — 6 blocks per day × 7 days
    NBLK = 6
    anc_v = {(p, b): pulp.LpVariable(f"anc_{p}_b{b}", lowBound=0, upBound=P_MAX)
             for p in ALL_P for b in range(HORIZON_DAYS * NBLK)}

    # Objective
    obj_terms = []
    for i in range(H):
        da_p   = horizon_data["da_pred"][i]
        sbp_p  = horizon_data["sbp_pred"][i]
        sp_p   = horizon_data["spike_prob"][i]
        da_eff = da_p + SPIKE_PREMIUM * sp_p
        # DA revenue
        obj_terms.append(da_eff * da_v[i] * DT)
        # Imbalance leg: scored at predicted SBP
        obj_terms.append(sbp_p * (pd_v[i] - pc_v[i] - da_v[i]) * DT)
        # Degradation
        obj_terms.append(-DEGRAD_COST * (pd_v[i] + pc_v[i]) * DT)
    # Ancillary revenue — Phase 1c bid-price optimisation.
    # For each (product, block-in-horizon) the LP picks the bid level that
    # maximises expected revenue under a Gaussian(μ, σ) clearing model with
    # a volume-constraint cap. The coefficient produced is
    #     E[rev per MW per hour] = P(accept|k*) × E[clearing|accept, k*]
    # where k* is the argmax level. Falls back to VOLUME_CAP × μ when σ is
    # unavailable (equivalent to top-quartile empirical rate).
    for (p, b), v in anc_v.items():
        mu = horizon_data["anc_pred"][(p, b)]
        si = horizon_data.get("anc_sigma", {}).get((p, b), 0.0)
        coef, _k = _bid_optimal_coef(mu, si, util_pen=UTIL_PENALTY[p])
        obj_terms.append(coef * v * 4.0)
    # DA-execution slippage: bid-ask + market-impact cost on both charge and
    # discharge legs. Slippage is symmetric so we subtract SLIPPAGE × (|pd|+|pc|)
    # × 0.5h from the objective. Because pd, pc ≥ 0 by construction, no abs()
    # transform is needed — LP variables already represent physical MW.
    for i in range(H):
        obj_terms.append(-SLIPPAGE_GBP_MWH * (pd_v[i] + pc_v[i]) * DT)
    prob += pulp.lpSum(obj_terms)

    # SoC dynamics
    prob += soc_v[0] == soc_init
    for i in range(H):
        prob += soc_v[i + 1] == soc_v[i] + ETA * pc_v[i] * DT - pd_v[i] * DT / ETA

    # Capacity-sharing per period.
    # Ancillary products of a given direction consume the same physical headroom
    # as wholesale dispatch on the same side. H-suffix products (charge-side
    # response) share MW with wholesale CHARGE; L-suffix products (discharge-side
    # response) share MW with wholesale DISCHARGE. Prior implementations had
    # these mappings inverted, which allowed the LP to double-book discharge
    # capacity (e.g. bidding 50 MW DR-L while also dispatching 50 MW wholesale
    # discharge). Fixed 2026-07-09.
    for i in range(H):
        block_in_horizon = (i // 8)    # 0..(7*6-1)
        prob += pc_v[i] + pulp.lpSum(anc_v[(p, block_in_horizon)] for p in HIGH) <= P_MAX
        prob += pd_v[i] + pulp.lpSum(anc_v[(p, block_in_horizon)] for p in LOW ) <= P_MAX

    # Imbalance cap — Phase 1 + Phase 2E risk controls.
    # Per-SP effective caps depend on:
    #   1. Asymmetry: long > short (lean into SBP spike upside, limit downside)
    #   2. Spike-prob gate: shrink cap as Model C spike_prob rises
    #   3. Spread gate: force imb = 0 when |DA_pred − SBP_pred| < IMB_MIN_SPREAD
    #   4. (Phase 2E) Uncertainty gate: shrink cap as Model B quantile width
    #      rises — wide P5-P95 means the LP's directional bet is unreliable
    for i in range(H):
        sbp_p = horizon_data["sbp_pred"][i]
        da_p  = horizon_data["da_pred"][i]
        spike_p = horizon_data["spike_prob"][i]
        # uncertainty_width is provided when Model B is the quantile flavour;
        # it falls back to 0 (no shrink) when the LP runs against the legacy
        # point-MSE Model B (backward compatibility with Phase 1).
        uw = horizon_data.get("uncertainty_width", [0.0] * H)[i]
        # Spike-gate multiplier in [0, 1]; full shrink when spike_p ≥ 1/IMB_SPIKE_PENALTY
        spike_mult = max(0.0, 1.0 - spike_p * IMB_SPIKE_PENALTY)
        # Spread gate: kill all imbalance freedom when the predicted DA-SBP edge
        # is below threshold (signal swamped by forecast error)
        spread_mult = 1.0 if abs(da_p - sbp_p) >= IMB_MIN_SPREAD else 0.0
        # Phase 2E: uncertainty-width gate
        uncert_mult  = max(0.0, 1.0 - uw / IMB_UNCERTAINTY_REF)
        gate = spike_mult * spread_mult * uncert_mult
        eff_up   = IMB_LIMIT_UP   * gate
        eff_down = IMB_LIMIT_DOWN * gate
        # imbalance = pd - pc - da_v
        prob += pd_v[i] - pc_v[i] - da_v[i] <=  eff_up
        prob += pd_v[i] - pc_v[i] - da_v[i] >= -eff_down

    # Cycle cap — per day (sum of discharge ≤ MAX_CYCLES × E_max)
    for day in range(HORIZON_DAYS):
        i0, i1 = day * 48, (day + 1) * 48
        prob += pulp.lpSum(pd_v[i] * DT for i in range(i0, i1)) <= MAX_CYCLES * E_MAX

    # Solve
    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=120)
    status = prob.solve(solver)
    if pulp.LpStatus[status] not in ("Optimal", "Optimal Tolerance"):
        return None

    # Extract DAY 1 only
    out = {
        "pd": [pd_v[i].value() or 0.0 for i in range(48)],
        "pc": [pc_v[i].value() or 0.0 for i in range(48)],
        "da": [da_v[i].value() or 0.0 for i in range(48)],
        # End-of-period SoC trajectory for Day 1 (soc[1..48] in MWh)
        "soc": [soc_v[i + 1].value() if soc_v[i + 1].value() is not None else soc_init
                for i in range(48)],
        "soc_end": soc_v[48].value() or soc_init,
        "anc_d1": {p: [anc_v[(p, b)].value() or 0.0 for b in range(NBLK)] for p in ALL_P},
    }
    return out


def main():
    log("=" * 78); log("LP v6 ensemble — multi-day rolling + imbalance"); log("=" * 78)

    log("Loading master + all forecast parquets…")
    df = pd.read_parquet(MASTER, engine="fastparquet")
    ma = pd.read_parquet(MODEL_A, engine="fastparquet")
    mb = pd.read_parquet(MODEL_B, engine="fastparquet")
    mc = pd.read_parquet(MODEL_C, engine="fastparquet")
    md = pd.read_parquet(MODEL_D, engine="fastparquet")
    log(f"  master: {len(df):,}, A: {len(ma):,}, B: {len(mb):,}, C: {len(mc):,}, D: {len(md):,}")

    # Build wide pivot tables for fast lookup
    ma["date"] = pd.to_datetime(ma["date"])
    md["date"] = pd.to_datetime(md["date"])
    da_pred_w  = ma.set_index(["date", "period"])["predicted"].unstack()
    sbp_pred_w = mb.copy(); sbp_pred_w["date"] = pd.to_datetime(sbp_pred_w.index.date)
    sbp_pred_w["period"] = ((sbp_pred_w.index.tz_convert("Europe/London").hour * 2 +
                              sbp_pred_w.index.tz_convert("Europe/London").minute // 30) + 1)
    # Phase 2E: if Model B is the quantile flavour, pivot the uncertainty width
    # into a wide table for per-SP lookup. Falls back to a zero-width DataFrame
    # if Model B is still point-MSE (no shrink applied → matches Phase 1).
    if "uncertainty_width" in mb.columns:
        uw_w = sbp_pred_w.pivot_table(index="date", columns="period", values="uncertainty_width")
        log(f"  Model B uncertainty_width loaded: mean={uw_w.values[~np.isnan(uw_w.values)].mean():.1f} £/MWh")
    else:
        uw_w = pd.DataFrame()
        log("  Model B is point-MSE — Phase 2E uncertainty gate disabled")
    sbp_pred_w = sbp_pred_w.pivot_table(index="date", columns="period", values="predicted_sbp")
    spike_w = mc.copy(); spike_w["date"] = pd.to_datetime(spike_w.index.date)
    spike_w["period"] = ((spike_w.index.tz_convert("Europe/London").hour * 2 +
                          spike_w.index.tz_convert("Europe/London").minute // 30) + 1)
    spike_w = spike_w.pivot_table(index="date", columns="period", values="prob")

    # Actuals from master (for revenue scoring)
    df["date"] = pd.to_datetime(df["settlement_date"])
    df["sp"]   = df["settlement_period"]
    da_act_w  = df.pivot_table(index="date", columns="sp", values="da_price")
    sbp_act_w = df.pivot_table(index="date", columns="sp", values="sbp")
    # EFA-aligned ancillary actuals — wide table per product, indexed by EFA-end-date
    # with columns = EFA block 1..6. Uses representative SP per block.
    anc_act_d = build_efa_anc_wide(df, ALL_P)

    # Model D per (product, day, efa_block): μ (P50) + σ (from quantile spread).
    # Phase 1c uses σ to compute a Gaussian bid-price-optimal coefficient.
    anc_pred_d  = {}
    anc_sigma_d = {}
    has_sigma_col = "sigma" in md.columns
    for prod in ALL_P:
        sub = md[md["product"] == prod]
        if not len(sub):
            anc_pred_d[prod]  = pd.DataFrame()
            anc_sigma_d[prod] = pd.DataFrame()
            continue
        anc_pred_d[prod]  = sub.pivot_table(index="date", columns="efa_block", values="predicted")
        if has_sigma_col:
            anc_sigma_d[prod] = sub.pivot_table(index="date", columns="efa_block", values="sigma")
        else:
            anc_sigma_d[prod] = pd.DataFrame()
    if not has_sigma_col:
        log("  ! Model D predictions have no 'sigma' column — Phase 1c will use "
            "volume-cap fallback (equivalent to top-quartile empirical rate).")

    # Iterate days
    test_dates = pd.date_range(TEST_START, TEST_END, freq="D")
    soc_state  = 0.5 * E_MAX
    dispatch_rows = []
    revenue_rows  = []
    anc_rows      = []
    n_skip = 0
    for di, d in enumerate(test_dates):
        # Build horizon arrays (7 EFA days). Each EFA day starts at 23:00 of
        # the previous calendar date and ends at 23:00 of the EFA-end-date.
        try:
            da_pred  = []
            sbp_pred = []
            spike_p  = []
            uw_pred  = []   # Phase 2E uncertainty width (or zeros for legacy MB)
            for day_offset in range(HORIZON_DAYS):
                dh = d + pd.Timedelta(days=day_offset)
                # DA — forecast (Model A ensemble), EFA-day-aligned
                arr = efa_fetch_array(da_pred_w, dh, fill=np.nan)
                # Fill any remaining NaNs with the day's mean, fallback to 50
                if np.all(np.isnan(arr)):
                    arr = np.full(48, 50.0)
                else:
                    mean_val = np.nanmean(arr)
                    arr = np.where(np.isnan(arr), mean_val, arr)
                da_pred.extend(arr)
                # SBP — forecast (Model B); fallback to DA forecast
                sarr = efa_fetch_array(sbp_pred_w, dh, fill=np.nan)
                if np.all(np.isnan(sarr)):
                    sarr = arr.copy()
                else:
                    sarr = np.where(np.isnan(sarr), arr, sarr)
                sbp_pred.extend(sarr)
                # Spike prob — Model C; fallback 0
                parr = efa_fetch_array(spike_w, dh, fill=0.0)
                spike_p.extend(parr)
                # Phase 2E: Model B uncertainty width; fallback 0 (no shrink)
                if not uw_w.empty:
                    uarr = efa_fetch_array(uw_w, dh, fill=0.0)
                    uarr = np.where(np.isnan(uarr), 0.0, uarr)
                else:
                    uarr = np.zeros(48)
                uw_pred.extend(uarr)
            # Ancillary μ + σ per (product, block-in-horizon). Phase 1c uses σ
            # for bid-price optimisation.
            anc_pred_h  = {}
            anc_sigma_h = {}
            for p in ALL_P:
                mu_vals, si_vals = [], []
                for day_offset in range(HORIZON_DAYS):
                    dh = d + pd.Timedelta(days=day_offset)
                    if dh in anc_pred_d[p].index:
                        mu_v = anc_pred_d[p].loc[dh].reindex(range(1, 7)).fillna(0).values
                    else:
                        mu_v = np.zeros(6)
                    mu_vals.extend(mu_v)
                    if not anc_sigma_d[p].empty and dh in anc_sigma_d[p].index:
                        si_v = anc_sigma_d[p].loc[dh].reindex(range(1, 7)).fillna(0).values
                    else:
                        si_v = np.zeros(6)
                    si_vals.extend(si_v)
                for b_idx, (mu_v, si_v) in enumerate(zip(mu_vals, si_vals)):
                    anc_pred_h[(p, b_idx)]  = float(mu_v)
                    anc_sigma_h[(p, b_idx)] = float(si_v)

            horizon = {
                "da_pred":           da_pred,
                "sbp_pred":          sbp_pred,
                "spike_prob":        spike_p,
                "uncertainty_width": uw_pred,
                "anc_pred":          anc_pred_h,
                "anc_sigma":         anc_sigma_h,
            }

            result = solve_horizon(d, horizon, soc_state)
            if result is None:
                n_skip += 1
                continue

            # Score realised revenue against EFA-day actuals (BACKTEST behaviour)
            # if any actuals exist for the EFA day; otherwise emit dispatch
            # without scoring (OPERATIONAL forward day).
            da_act_d  = efa_fetch_array(da_act_w,  d, fill=np.nan)
            sbp_act_d = efa_fetch_array(sbp_act_w, d, fill=np.nan)
            have_da_actuals  = not np.all(np.isnan(da_act_d))
            have_sbp_actuals = not np.all(np.isnan(sbp_act_d))

            da_rev   = 0.0
            imb_rev  = 0.0
            deg      = 0.0
            slippage = 0.0
            for i in range(48):
                pd_i, pc_i, da_i = result["pd"][i], result["pc"][i], result["da"][i]
                if not np.isnan(da_act_d[i]):
                    da_rev  += da_act_d[i]  * da_i * DT
                if not np.isnan(sbp_act_d[i]):
                    imb_rev += sbp_act_d[i] * (pd_i - pc_i - da_i) * DT
                deg += DEGRAD_COST * (pd_i + pc_i) * DT
                # Phase 4 (2026-07-10): execution slippage as bid-ask friction
                # on both dispatch legs. Symmetric — subtracts from headline.
                slippage += SLIPPAGE_GBP_MWH * (pd_i + pc_i) * DT
                # Dispatch row — EFA-day-keyed. period 1 = 23:00 of (d-1).
                dispatch_rows.append({"date": d, "period": i + 1,
                                      "pd_mw": pd_i, "pc_mw": pc_i,
                                      "net_mw": pd_i - pc_i, "da_pos_mw": da_i,
                                      "soc_mwh": result["soc"][i]})

            # Ancillary commitments — always emitted; clearing actuals where available.
            # Realised revenue matches the LP objective's Phase 1c coefficient:
            # bid-price-optimal expected revenue given the LP's forecast (μ, σ).
            # This is consistent with what the LP optimised for — the "actual"
            # clearing enters only as a validation reference (via clearing_actual
            # column) not as a separate scoring model.
            anc_rev = 0.0
            for p in ALL_P:
                blk_actuals = (anc_act_d[p].loc[d].reindex(range(1, 7)).fillna(0).values
                               if d in anc_act_d[p].index else np.zeros(6))
                # Also fetch the μ, σ from Model D for this day so the realised
                # coefficient matches the objective's decision context.
                mu_d = (anc_pred_d[p].loc[d].reindex(range(1, 7)).fillna(0).values
                        if d in anc_pred_d[p].index else np.zeros(6))
                si_d = (anc_sigma_d[p].loc[d].reindex(range(1, 7)).fillna(0).values
                        if (not anc_sigma_d[p].empty and d in anc_sigma_d[p].index)
                        else np.zeros(6))
                for b in range(6):
                    mw = result["anc_d1"][p][b]
                    # Phase 1c: use the same bid-optimal coefficient the LP used
                    # when deciding to bid. Realized clearing is stored as
                    # 'clearing_actual' for validation but not multiplied here.
                    coef, k_best = _bid_optimal_coef(float(mu_d[b]),
                                                    float(si_d[b]),
                                                    util_pen=UTIL_PENALTY[p])
                    anc_rev += mw * 4.0 * coef
                    anc_rows.append({"date": d, "product": p, "efa_block": b + 1, "mw": mw,
                                     "clearing_actual": float(blk_actuals[b]),
                                     "mu_pred":         float(mu_d[b]),
                                     "sigma_pred":      float(si_d[b]),
                                     "bid_level_k":     int(k_best),
                                     "util_penalty":    UTIL_PENALTY[p],
                                     "effective_rate":  coef / max(float(mu_d[b]), 1e-9)})

            # Revenue rows only for EFA days with actuals.
            if have_da_actuals and have_sbp_actuals:
                total = da_rev + imb_rev + anc_rev - deg - slippage
                revenue_rows.append({"date": d, "da": da_rev, "imb": imb_rev,
                                     "anc": anc_rev, "deg": deg,
                                     "slippage": slippage, "total": total})
            soc_state = result["soc_end"]

            if (di + 1) % 50 == 0:
                log(f"  day {di+1}/{len(test_dates)} ({d.date()}): solved={len(revenue_rows):,} skipped={n_skip:,}")

        except Exception as ex:
            log(f"  WARN day {d.date()}: {ex}")
            n_skip += 1
            continue

    disp_df = pd.DataFrame(dispatch_rows)
    rev_df  = pd.DataFrame(revenue_rows)
    anc_df  = pd.DataFrame(anc_rows)
    disp_df.to_parquet(OUT_DISP); rev_df.to_parquet(OUT_REV); anc_df.to_parquet(OUT_ANC)
    log(f"Saved {OUT_DISP}, {OUT_REV}, {OUT_ANC}")
    log(f"Days dispatched: {len(rev_df):,}  skipped: {n_skip:,}")

    # Eval
    if len(rev_df) > 0:
        with open(OUT_EVAL, "w", encoding="utf-8") as fout:
            fout.write("=" * 70 + "\n")
            fout.write("LP v6 ENSEMBLE — eval (revenue at ACTUAL prices)\n")
            fout.write("=" * 70 + "\n")
            fout.write(f"Days dispatched: {len(rev_df):,}\n")
            fout.write(f"Total revenue:   GBP {rev_df['total'].sum():,.0f}\n")
            fout.write(f"  DA leg:        GBP {rev_df['da'].sum():,.0f}\n")
            fout.write(f"  Imbalance:     GBP {rev_df['imb'].sum():,.0f}\n")
            fout.write(f"  Ancillary:     GBP {rev_df['anc'].sum():,.0f}\n")
            fout.write(f"  Degradation:   GBP {rev_df['deg'].sum():,.0f}\n\n")
            rev_df["year"] = pd.to_datetime(rev_df["date"]).dt.year
            fout.write("Per-year revenue:\n")
            for yr in sorted(rev_df["year"].unique()):
                g = rev_df[rev_df["year"] == yr]
                fout.write(f"  {yr}: days={len(g):3d}  total=GBP {g['total'].sum():>12,.0f}  "
                           f"DA={g['da'].sum():>11,.0f}  imb={g['imb'].sum():>10,.0f}  anc={g['anc'].sum():>10,.0f}\n")
        log(f"Eval: {OUT_EVAL}")


if __name__ == "__main__":
    main()
