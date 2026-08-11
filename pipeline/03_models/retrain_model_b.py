"""
Clean Pipeline — Stage 3d: Model B — XGBoost SBP imbalance-price forecaster.

PHASE 2A: QUANTILE REGRESSION.
  Instead of a single point-MSE estimate, train one model per quantile
  (P5 / P25 / P50 / P75 / P95). XGBoost 2.0+ supports this natively via
  `reg:quantileerror` with `quantile_alpha=q`.

  Backward-compatible: the output parquet still has `predicted_sbp` (=P50),
  so downstream LP code that reads this column keeps working. The Phase 2
  LP reads the additional quantile columns to size imbalance positions
  proportional to uncertainty width.

Walk-forward train at each monthly boundary. Per-half-hour XGBoost regressor
trained 5x (once per quantile).

CRITICAL leakage rules baked in:
  - DA feature is the ENSEMBLE Model A PREDICTION (not the actual). Reason:
    when the operational LP calls Model B at NESO EAC gate closure (~09:00 D-1),
    the actual DA has NOT yet cleared (EPEX clears 11:00 D-1). So Model B must
    use a forecast of DA, not the cleared price.

  - All lag features use proper shifts to avoid same-period information.

Input:   data/processed/master.parquet
         data/processed/model_a_ensemble_predictions.parquet
Output:  data/processed/model_b_predictions.parquet
         data/processed/model_b_eval.txt
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
MODEL_A   = PROC / "model_a_ensemble_predictions.parquet"
OUT_PQ    = PROC / "model_b_predictions.parquet"
OUT_EVAL  = PROC / "model_b_eval.txt"

TEST_START = pd.Timestamp("2022-01-01", tz="UTC")
import os as _os
_env_end = _os.environ.get("LIVE_TEST_END", "").strip()
TEST_END   = (pd.Timestamp(_env_end + " 23:30:00", tz="UTC") if _env_end
              else pd.Timestamp("2026-05-31 23:30:00", tz="UTC"))
MIN_TRAIN  = 1500   # rows

# Phase 2A: quantile grid. P50 stays the headline forecast (backward-compat);
# P5-P95 used by Phase 2 LP for risk-aware position sizing.
QUANTILES = [0.05, 0.25, 0.50, 0.75, 0.95]
Q_COLS = {0.05: "p05_sbp", 0.25: "p25_sbp", 0.50: "predicted_sbp",
          0.75: "p75_sbp", 0.95: "p95_sbp"}

XGB_PARAMS_BASE = dict(
    max_depth=6, learning_rate=0.05, n_estimators=300,
    min_child_weight=1, tree_method="hist", n_jobs=-1, verbosity=0,
)

t0 = time.time()
def log(m): print(f"[{(time.time()-t0):6.1f}s] {m}", flush=True)


def pinball_loss(y_true: np.ndarray, y_pred: np.ndarray, q: float) -> float:
    """Quantile pinball loss for a single quantile."""
    err = y_true - y_pred
    return float(np.mean(np.maximum(q * err, (q - 1.0) * err)))


def main():
    log("=" * 78); log("MODEL B — QUANTILE XGBoost SBP (ensemble-DA feed) — Phase 2A"); log("=" * 78)
    log(f"  Quantiles trained: {QUANTILES}")

    log("Loading master + Model A ensemble predictions…")
    df = pd.read_parquet(MASTER, engine="fastparquet")
    ens = pd.read_parquet(MODEL_A, engine="fastparquet")

    ens["date"] = pd.to_datetime(ens["date"])
    df["date"]   = pd.to_datetime(df["settlement_date"])
    df["sp"]     = df["settlement_period"]
    da_map = ens.set_index(["date", "period"])["predicted"].rename("predicted_da")
    df = df.join(da_map, on=["date", "sp"])
    log(f"  ensemble-DA non-null in master: {df['predicted_da'].notna().sum():,}/{len(df):,}")

    # ---- Features (unchanged from Phase 0 — Path-2 / lag-96 safe) ----
    log("Building Model B features (operationally-safe, lag-96 minimum)…")
    f = pd.DataFrame(index=df.index)
    f["target_sbp"] = df["sbp"]
    f["predicted_da"]            = df["predicted_da"]
    f["sbp_lag_96"]              = df["sbp"].shift(96)
    f["sbp_lag_336"]             = df["sbp"].shift(336)
    f["niv_lag_96"]              = df["niv"].shift(96)
    f["niv_lag_336"]             = df["niv"].shift(336)
    f["demand_error_lag96"]      = (df["actual_demand_mw"] - df["da_demand_forecast_mw"]).shift(96)
    f["wind_actual_lag96"]       = df["wind_actual_proxy_mw"].shift(96)
    f["wind_forecast_lag96"]     = df["da_wind_forecast_mw"].shift(96)
    f["wind_error_lag96"]        = f["wind_actual_lag96"] - f["wind_forecast_lag96"]
    f["boalf_total_mw_lag96"]    = df["boalf_total_mw"].shift(96)
    f["boalf_so_count_lag96"]    = df["boalf_so_count"].shift(96)
    f["boalf_offer_count_lag96"] = df["boalf_offer_count"].shift(96)
    f["boalf_bid_count_lag96"]   = df["boalf_bid_count"].shift(96)
    f["gen_pct_gas_lag96"]       = df["gen_pct_gas"].shift(96)
    f["gen_pct_wind_lag96"]      = df["gen_pct_wind"].shift(96)
    f["gen_pct_solar_lag96"]     = df["gen_pct_solar"].shift(96)
    f["gen_pct_nuclear_lag96"]   = df["gen_pct_nuclear"].shift(96)
    f["sbp_roll7d_mean"]         = df["sbp_roll7d_mean"].shift(96)
    f["sbp_roll7d_std"]          = df["sbp_roll7d_std"].shift(96)
    f["niv_roll7d_mean"]         = df["niv_roll7d_mean"].shift(96)
    f["hour"]                    = df["hour"]
    f["day_of_week"]             = df["day_of_week"]
    f["is_weekend"]              = df["is_weekend"]
    f["is_holiday"]              = df["is_holiday"]
    f["settlement_period"]       = df["settlement_period"]
    # Phase 2B new features — lag-96 safe.
    # Interconnector net flow: system-tightness signal.
    # Sell/buy price adjustments + reserve scarcity premium: BSAD-derived
    # marginal cashout drivers that directly correlate with SBP spikes.
    if "ic_net_flow_mw" in df.columns:
        f["ic_net_flow_lag96"]   = df["ic_net_flow_mw"].shift(96)
        f["ic_net_flow_lag336"]  = df["ic_net_flow_mw"].shift(336)
    if "sellPriceAdjustment" in df.columns:
        f["sell_adj_lag96"]      = df["sellPriceAdjustment"].shift(96)
    if "buyPriceAdjustment" in df.columns:
        f["buy_adj_lag96"]       = df["buyPriceAdjustment"].shift(96)
    if "reserveScarcityPrice" in df.columns:
        f["scarcity_lag96"]      = df["reserveScarcityPrice"].shift(96)
    log(f"  feature matrix: {f.shape}")

    # ---- Walk-forward — train one model per quantile per month boundary ----
    # Phase 2C: Distinguish LEGACY features (always present, full history)
    # from NEW features (only present after the Phase 2B pull script started
    # populating them). For the dropna gate we only require legacy features;
    # the new features are fillna(0) so XGBoost can treat them as "missing"
    # via its native sparse-aware tree splits.
    NEW_FEATURES = [
        "ic_net_flow_lag96", "ic_net_flow_lag336",
        "sell_adj_lag96", "buy_adj_lag96", "scarcity_lag96",
    ]
    feature_cols = [c for c in f.columns if c not in ("target_sbp",)]
    legacy_cols  = [c for c in feature_cols if c not in NEW_FEATURES]
    new_present  = [c for c in NEW_FEATURES if c in feature_cols]
    log(f"  legacy features: {len(legacy_cols)}, new features (with fillna): {len(new_present)}")

    retrain_dates = pd.date_range(TEST_START, TEST_END, freq="MS", tz="UTC")
    log(f"  retrain dates: {len(retrain_dates)}")
    log(f"  total models to train: {len(retrain_dates)} months × {len(QUANTILES)} quantiles")

    all_preds = []
    for ri, rd in enumerate(retrain_dates):
        next_rd = (retrain_dates[ri + 1] if ri + 1 < len(retrain_dates)
                   else TEST_END + pd.Timedelta(days=1))
        train_mask = f.index < rd
        test_mask  = (f.index >= rd) & (f.index < next_rd)
        # Gate on legacy features only (must be present)
        train = f[train_mask].dropna(subset=legacy_cols + ["target_sbp"]).copy()
        test  = f[test_mask].dropna(subset=legacy_cols).copy()
        # Fill new features with 0 — XGBoost histogram method treats them as
        # missing during split-finding (sparse-aware learning)
        for c in new_present:
            train[c] = train[c].fillna(0.0)
            test[c]  = test[c].fillna(0.0)
        if len(train) < MIN_TRAIN:
            continue
        if len(test) == 0:
            continue

        # Train one model per quantile
        chunk_data = {"actual_sbp": test["target_sbp"].values}
        for q in QUANTILES:
            params = dict(XGB_PARAMS_BASE)
            params["objective"]      = "reg:quantileerror"
            params["quantile_alpha"] = q
            model = xgb.XGBRegressor(**params)
            model.fit(train[feature_cols].values, train["target_sbp"].values)
            preds = model.predict(test[feature_cols].values)
            chunk_data[Q_COLS[q]] = preds

        # Enforce monotonicity (rare crossings can happen with independent models)
        # Sort the per-row quantile predictions; preserves the headline P50 column
        # name while guaranteeing P5 ≤ P25 ≤ P50 ≤ P75 ≤ P95.
        stacked = np.stack([chunk_data[Q_COLS[q]] for q in QUANTILES], axis=1)
        stacked.sort(axis=1)
        for i, q in enumerate(QUANTILES):
            chunk_data[Q_COLS[q]] = stacked[:, i]

        out_chunk = pd.DataFrame(chunk_data, index=test.index)
        all_preds.append(out_chunk)

        if (ri + 1) % 6 == 0:
            log(f"  [{ri+1:>2}/{len(retrain_dates)}] {rd.strftime('%Y-%m')}: "
                f"cumulative {sum(len(x) for x in all_preds):,}")

    out = pd.concat(all_preds, axis=0)
    out.index.name = "timestamp"
    # Derived: uncertainty width as the LP-facing scalar
    out["uncertainty_width"] = out["p95_sbp"] - out["p05_sbp"]

    if OUT_PQ.exists():
        shutil.copy2(OUT_PQ, OUT_PQ.with_suffix(".bak.parquet"))
    out.to_parquet(OUT_PQ)
    log(f"Saved {OUT_PQ} ({len(out):,} rows)")

    # ---- Eval: per-quantile pinball + coverage + headline MAE/RMSE ----
    log("Computing eval metrics…")
    actual = out["actual_sbp"].values
    out["err_p50"] = (out["actual_sbp"] - out["predicted_sbp"]).abs()
    out["year"]   = out.index.year

    overall = {}
    for q in QUANTILES:
        col = Q_COLS[q]
        overall[f"pinball_q{int(q*100):02d}"] = pinball_loss(actual, out[col].values, q)
    # Coverage: where actual falls within P5-P95 (should be ~90%) and P25-P75 (~50%)
    cov_90 = float(np.mean((actual >= out["p05_sbp"].values) & (actual <= out["p95_sbp"].values)))
    cov_50 = float(np.mean((actual >= out["p25_sbp"].values) & (actual <= out["p75_sbp"].values)))

    with open(OUT_EVAL, "w", encoding="utf-8") as fout:
        fout.write("=" * 70 + "\n")
        fout.write("MODEL B — eval (Quantile XGBoost, ensemble-DA feed) — Phase 2A\n")
        fout.write("=" * 70 + "\n")
        fout.write(f"Total predictions: {len(out):,}\n\n")
        fout.write(f"P50 MAE:           {out['err_p50'].mean():.2f}\n")
        fout.write(f"P50 RMSE:          {float(np.sqrt((out['err_p50']**2).mean())):.2f}\n")
        fout.write(f"Mean uncertainty width (P95-P05): {out['uncertainty_width'].mean():.2f} £/MWh\n\n")
        fout.write("Per-quantile PINBALL loss (lower is better, perfect quantile = 0):\n")
        for q in QUANTILES:
            fout.write(f"  Q{int(q*100):02d}:  {overall[f'pinball_q{int(q*100):02d}']:.3f}\n")
        fout.write(f"\nCoverage (should approach nominal):\n")
        fout.write(f"  P5-P95 (90% nominal): {cov_90*100:.1f}%\n")
        fout.write(f"  P25-P75 (50% nominal): {cov_50*100:.1f}%\n\n")
        fout.write("Per-year P50 MAE:\n")
        for yr in sorted(out["year"].unique()):
            grp = out[out["year"] == yr]
            fout.write(f"  {yr}: MAE {grp['err_p50'].mean():.2f}  N={len(grp):,}\n")
    log(f"Eval: {OUT_EVAL}")


if __name__ == "__main__":
    main()
