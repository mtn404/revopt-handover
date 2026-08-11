"""
Clean Pipeline — Stage 3f: Model D — per-product ancillary clearing-price forecaster.

QUANTILE XGBoost, one model per (product × quantile). Trains at 5 quantiles
per product per monthly walk-forward step to expose the full clearing-price
distribution, then the LP's bid-price optimiser can size its bid strategically.

Output: predicted (P50), p05, p25, p75, p95 £/MW/h per (product, settlement_date,
efa_block), plus derived `sigma` = (p95 - p05) / (2 × 1.645) — the Gaussian-
equivalent standard deviation, used by the LP for bid-level construction.

CRITICAL leakage rule:
  - Same-day DA-price features DROPPED.
    Reason: NESO EAC clears ~09:00 D-1; EPEX DA clears ~11:00 D-1.
    Same-day DA features would be a 2-hour future leak.
  - Only PRIOR-day DA aggregates are allowed.

Input:   data/processed/master.parquet
Output:  data/processed/model_d_predictions.parquet
         data/processed/model_d_eval.txt

Phase 1 (2026-07-10) refactor:  Point-MSE XGBoost → quantile XGBoost. Adds
sigma. Preserves the `predicted` column (== P50) for LP backward-compatibility.
"""

import sys, time, shutil, warnings
from pathlib import Path
import pandas as pd
import numpy as np
import xgboost as xgb

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")

HERE      = Path(__file__).resolve().parent
PROC      = HERE.parent / "data" / "processed"
MASTER    = PROC / "master.parquet"
OUT_PQ    = PROC / "model_d_predictions.parquet"
OUT_EVAL  = PROC / "model_d_eval.txt"

PRODUCTS   = ["dch", "dcl", "dmh", "dml", "drh", "drl"]
TEST_START = pd.Timestamp("2022-01-01", tz="UTC")
import os as _os
_env_end = _os.environ.get("LIVE_TEST_END", "").strip()
TEST_END   = (pd.Timestamp(_env_end, tz="UTC") if _env_end
              else pd.Timestamp("2026-05-31", tz="UTC"))
MIN_TRAIN  = 300

# Quantile grid (matches Model B for cross-model consistency).
# P50 is the headline forecast; P05/P95 anchor the tails; P25/P75 quality-check.
QUANTILES = [0.05, 0.25, 0.50, 0.75, 0.95]
Q_COLS = {0.05: "p05", 0.25: "p25", 0.50: "predicted",
          0.75: "p75", 0.95: "p95"}
# Gaussian-equivalent std from (P95-P05) range: 2 × 1.6449 ≈ 3.29 std between
# the 5th and 95th percentile of a standard normal.
SIGMA_DIVISOR = 2.0 * 1.6449

XGB_PARAMS_BASE = dict(
    max_depth=5, learning_rate=0.05, n_estimators=300,
    min_child_weight=3, tree_method="hist", n_jobs=-1, verbosity=0,
)


def pinball_loss(y_true: np.ndarray, y_pred: np.ndarray, q: float) -> float:
    """Quantile pinball loss for a single quantile."""
    err = y_true - y_pred
    return float(np.mean(np.maximum(q * err, (q - 1.0) * err)))

t0 = time.time()
def log(m): print(f"[{(time.time()-t0):6.1f}s] {m}", flush=True)


def build_efa_grid(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse the half-hourly master into one row per (date, efa_block)
    with clearing prices and aggregated features.

    EFA-day block assignment (within calendar day D, after the EFA-day refactor):
      Block 1: SP 1-6   (the 03:00-cap part of EFA day D's block 1, which
                         also includes SP 47-48 of D-1; we use SP 1-6 of D
                         as the in-day proxy since the clearing is identical)
      Block 2: SP 7-14
      Block 3: SP 15-22
      Block 4: SP 23-30
      Block 5: SP 31-38
      Block 6: SP 39-46
      SP 47-48 of D belong to EFA day D+1's block 1 → dropped here so we
      don't double-count; they'll be picked up via D+1's block-1 row.

    LEAK-FREE INVARIANT (auction at 09:00 D-1; delivery day D):
      - demand_fc / wind_fc are DAY-AHEAD FORECASTS issued for day D and
        known at the D-1 09:00 gate. ✓ Same-day OK.
      - All other "current-day" features (gen mix, gas, etc.) are SHIFTED by 1
        day so the model only sees data through D-1 at decision time.
    """
    def _sp_to_efa_block(sp: int):
        if sp <= 6:  return 1
        if sp <= 14: return 2
        if sp <= 22: return 3
        if sp <= 30: return 4
        if sp <= 38: return 5
        if sp <= 46: return 6
        return None  # SP 47-48 → next EFA day's block 1 (skipped here)
    g = df.copy()
    g["efa_block"] = g["settlement_period"].map(_sp_to_efa_block)
    g = g[g["efa_block"].notna()].copy()
    g["efa_block"] = g["efa_block"].astype(int)
    g["date"]      = pd.to_datetime(g["settlement_date"])

    # Day-D-known features (forecasts published at D-1 gate closure)
    agg = g.groupby(["date", "efa_block"]).agg(
        anc_dch=("anc_dch_price", "first"), anc_dcl=("anc_dcl_price", "first"),
        anc_dmh=("anc_dmh_price", "first"), anc_dml=("anc_dml_price", "first"),
        anc_drh=("anc_drh_price", "first"), anc_drl=("anc_drl_price", "first"),
        demand_fc=("da_demand_forecast_mw", "mean"),     # ← known at D-1 gate ✓
        wind_fc=  ("da_wind_forecast_mw",   "mean"),     # ← known at D-1 gate ✓
        day_of_week=("day_of_week", "first"),
        is_weekend=("is_weekend", "first"),
        is_holiday=("is_holiday", "first"),
        month=("month", "first"),
    ).reset_index()

    # Prior-day SHIFTED features for things only knowable at D-1
    gen_pct_gas_daily = g.groupby("date")["gen_pct_gas"].mean()
    gas_nbp_daily     = g.groupby("date")["gas_nbp_p_therm"].first()
    agg = agg.merge(gen_pct_gas_daily.shift(1).rename("gen_pct_gas_lag1"),
                    left_on="date", right_index=True, how="left")
    agg = agg.merge(gas_nbp_daily.shift(1).rename("gas_yest"),
                    left_on="date", right_index=True, how="left")

    # Prior-day DA summary (NO same-day DA features)
    da_daily = df.pivot_table(index=pd.to_datetime(df["settlement_date"]),
                               columns="settlement_period", values="da_price")
    da_yest_max = da_daily.shift(1).max(axis=1, skipna=True).rename("da_yest_max")
    da_yest_mean = da_daily.shift(1).mean(axis=1, skipna=True).rename("da_yest_mean")
    agg = agg.merge(da_yest_max,  left_on="date", right_index=True, how="left")
    agg = agg.merge(da_yest_mean, left_on="date", right_index=True, how="left")

    # Prior-day spike counts
    spikes_daily = df.groupby(pd.to_datetime(df["settlement_date"]))["spike_label_200"].sum()
    agg = agg.merge(spikes_daily.shift(1).rename("yest_n_spikes"),
                    left_on="date", right_index=True, how="left")
    agg = agg.merge(spikes_daily.shift(7).rolling(7).mean().rename("rollwk_spike_rate"),
                    left_on="date", right_index=True, how="left")

    # Sort by date within each block so the groupby-shift is temporally correct
    agg = agg.sort_values(["efa_block", "date"]).reset_index(drop=True)

    # Per-product lag-1-day and lag-7-day clearing prices (within same efa_block)
    for prod in PRODUCTS:
        col = f"anc_{prod}"
        agg[f"{prod}_lag1"]    = agg.groupby("efa_block")[col].shift(1)
        agg[f"{prod}_lag7"]    = agg.groupby("efa_block")[col].shift(7)
        agg[f"{prod}_roll7d"]  = agg.groupby("efa_block")[col].transform(
            lambda s: s.shift(1).rolling(7, min_periods=1).mean())

    return agg


def main():
    log("=" * 78); log("MODEL D — per-product ancillary forecaster (leak-free)"); log("=" * 78)
    df = pd.read_parquet(MASTER, engine="fastparquet")
    grid = build_efa_grid(df)
    log(f"  EFA grid: {len(grid):,} rows × {len(grid.columns)} cols")

    feature_cols = ["demand_fc", "wind_fc",          # DA forecasts (known at D-1 gate)
                    "gen_pct_gas_lag1",              # SHIFTED prior-day mean (leak-free)
                    "gas_yest",                      # SHIFTED prior-day gas value (leak-free)
                    "day_of_week", "is_weekend", "is_holiday", "month",
                    "efa_block",
                    "da_yest_max", "da_yest_mean",   # already shifted
                    "yest_n_spikes", "rollwk_spike_rate"]
    # Add per-product lag features (model trained on its OWN product's lags only)
    retrain_dates = pd.date_range(TEST_START, TEST_END, freq="MS", tz="UTC")
    log(f"  retrain dates: {len(retrain_dates)}")

    all_rows = []
    for prod in PRODUCTS:
        log(f"  --- Product {prod.upper()} ---")
        target_col = f"anc_{prod}"
        prod_feats = feature_cols + [f"{prod}_lag1", f"{prod}_lag7", f"{prod}_roll7d"]
        for ri, rd in enumerate(retrain_dates):
            next_rd = (retrain_dates[ri + 1] if ri + 1 < len(retrain_dates)
                       else TEST_END + pd.Timedelta(days=1))
            train = grid[grid["date"] < rd.tz_localize(None)].dropna(subset=prod_feats + [target_col])
            # Test set: only require non-NaN FEATURES (not target clearing
            # price). This lets us predict ancillary clearings for forward
            # days before they settle.
            test  = grid[(grid["date"] >= rd.tz_localize(None)) &
                         (grid["date"] <  next_rd.tz_localize(None))].dropna(subset=prod_feats)
            if len(train) < MIN_TRAIN or len(test) == 0:
                continue

            # Train one XGBoost regressor per quantile (5 models per retrain step).
            X_train = train[prod_feats].values
            y_train = train[target_col].values
            X_test  = test[prod_feats].values
            preds_by_q = {}
            for q in QUANTILES:
                params = dict(XGB_PARAMS_BASE)
                params["objective"]      = "reg:quantileerror"
                params["quantile_alpha"] = q
                m = xgb.XGBRegressor(**params)
                m.fit(X_train, y_train)
                preds_by_q[q] = m.predict(X_test)

            # Enforce monotonicity per-row: rare quantile crossings can occur
            # with independent per-quantile models. Sort the stack so
            # P05 ≤ P25 ≤ P50 ≤ P75 ≤ P95 always holds.
            stacked = np.stack([preds_by_q[q] for q in QUANTILES], axis=1)
            stacked.sort(axis=1)
            preds_by_q = {q: stacked[:, i] for i, q in enumerate(QUANTILES)}

            chunk = pd.DataFrame({
                "date":     test["date"].values,
                "efa_block":test["efa_block"].values,
                "product":  prod,
                "actual":   test[target_col].values,
                "predicted":preds_by_q[0.50],   # P50 as headline (LP backward-compat)
                "p05":      preds_by_q[0.05],
                "p25":      preds_by_q[0.25],
                "p75":      preds_by_q[0.75],
                "p95":      preds_by_q[0.95],
            })
            all_rows.append(chunk)
        log(f"    {prod}: cumulative {sum(len(x) for x in all_rows if x['product'].iloc[0] == prod):,}")

    out = pd.concat(all_rows, ignore_index=True)
    # Derived Gaussian-equivalent sigma. Non-negative by construction because
    # p95 ≥ p05 after the monotonicity sort above.
    out["sigma"] = (out["p95"] - out["p05"]) / SIGMA_DIVISOR

    if OUT_PQ.exists():
        shutil.copy2(OUT_PQ, OUT_PQ.with_suffix(".bak.parquet"))
    out.to_parquet(OUT_PQ)
    log(f"Saved {OUT_PQ} ({len(out):,} rows)")

    out["err"] = (out["actual"] - out["predicted"]).abs()
    out["year"] = pd.to_datetime(out["date"]).dt.year
    with open(OUT_EVAL, "w", encoding="utf-8") as fout:
        fout.write("=" * 70 + "\n")
        fout.write("MODEL D — eval (per-product QUANTILE XGBoost, leak-free)\n")
        fout.write("=" * 70 + "\n\n")
        fout.write("Per-product overall MAE (of P50):\n")
        for prod in PRODUCTS:
            g = out[out["product"] == prod]
            if len(g) == 0: continue
            fout.write(f"  {prod:>4}: MAE {g['err'].mean():.3f}  N={len(g):,}\n")
        fout.write("\nPer-year overall MAE (of P50):\n")
        for yr in sorted(out["year"].unique()):
            g = out[out["year"] == yr]
            fout.write(f"  {yr}: MAE {g['err'].mean():.3f}  N={len(g):,}\n")

        # Quantile diagnostics — pinball loss + empirical coverage of P05-P95
        fout.write("\nPer-product pinball loss per quantile (lower = better):\n")
        fout.write(f"  {'product':>7s}  {'P05':>8s}  {'P25':>8s}  {'P50':>8s}  {'P75':>8s}  {'P95':>8s}\n")
        for prod in PRODUCTS:
            g = out[out["product"] == prod].dropna(subset=["actual"])
            if len(g) == 0: continue
            row = [f"{prod:>7s}"]
            for q in QUANTILES:
                y = g["actual"].values
                yhat = g[Q_COLS[q]].values
                row.append(f"{pinball_loss(y, yhat, q):>8.3f}")
            fout.write("  " + "  ".join(row) + "\n")

        fout.write("\nEmpirical coverage of the P05-P95 interval "
                   "(nominal 90%):\n")
        for prod in PRODUCTS:
            g = out[out["product"] == prod].dropna(subset=["actual"])
            if len(g) == 0: continue
            covered = ((g["actual"] >= g["p05"]) & (g["actual"] <= g["p95"])).mean()
            mean_sigma = g["sigma"].mean()
            fout.write(f"  {prod:>4}: coverage {covered*100:>5.1f}%  mean_sigma "
                       f"{mean_sigma:>6.3f}  N={len(g):,}\n")
    log(f"Eval: {OUT_EVAL}")


if __name__ == "__main__":
    main()
