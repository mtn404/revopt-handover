"""
Clean Pipeline — Stage 3e: Model C — SBP spike-probability classifier.

XGBoost binary classifier — predicts P(SBP > £200/MWh) per half-hour.

CRITICAL leakage rule:
  - Drops same-day price summary stats (yest_n_spikes_q95 was the leaky feature
    in the original v5; v6 removed it). This implementation does NOT use it.

  - Spike threshold £200/MWh is a fixed constant (no quantile that requires
    look-ahead).

Input:   data/processed/master.parquet
Output:  data/processed/model_c_predictions.parquet
         data/processed/model_c_eval.txt
"""

import sys, time, shutil, warnings
from pathlib import Path
import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.metrics import f1_score, roc_auc_score, average_precision_score, precision_score, recall_score


# ---------------------------------------------------------------------------
# Phase 2G: Focal loss objective for XGBoost
# ---------------------------------------------------------------------------
# Standard binary cross-entropy gives equal weight to all errors. For rare-
# event classification (spike rate ~0.4% in 2024+), this leads to a model
# that's heavily biased toward the majority class. Focal loss (Lin et al.,
# 2017) down-weights well-classified examples and focuses gradient updates
# on hard / rare examples.
#
#   FL(p_t) = -α (1 - p_t)^γ log(p_t)
#
# Where p_t = p if y=1 else (1-p). With α=0.25 and γ=2.0 (defaults from the
# original paper), hard positives contribute (1-p)^2 weight: misclassified
# positives get 4-100× the gradient of easy negatives.

FOCAL_ALPHA = 0.25
FOCAL_GAMMA = 2.0

def _sigmoid(x):
    # Numerically stable sigmoid
    return np.where(x >= 0, 1.0 / (1.0 + np.exp(-x)), np.exp(x) / (1.0 + np.exp(x)))


def focal_loss_obj(preds: np.ndarray, dtrain: xgb.DMatrix):
    """Custom XGBoost objective: focal loss gradient + Hessian.

    Returns (gradient, hessian) per-sample arrays."""
    y = dtrain.get_label()
    p = _sigmoid(preds)
    # p_t: p if y=1 else (1-p)
    pt = np.where(y == 1, p, 1 - p)
    alpha_t = np.where(y == 1, FOCAL_ALPHA, 1 - FOCAL_ALPHA)
    g = FOCAL_GAMMA
    one_minus_pt = np.clip(1 - pt, 1e-12, 1)
    # Derived analytically; gradient w.r.t. logit
    common = alpha_t * one_minus_pt ** g
    grad_term = g * np.log(np.clip(pt, 1e-12, 1)) * pt + (pt - 1)
    grad = common * grad_term * np.where(y == 1, 1, -1)
    # Approximate Hessian (use the dominant term — keeps it positive-definite)
    hess = np.maximum(common * (pt * (1 - pt)) * (g + 1), 1e-6)
    return grad, hess


def find_optimal_threshold(y_true: np.ndarray, y_prob: np.ndarray, default: float = 0.5) -> float:
    """F1-maximising threshold via grid search."""
    if y_true.sum() == 0 or len(np.unique(y_true)) < 2:
        return default
    thresholds = np.linspace(0.01, 0.99, 99)
    best_f1, best_th = 0.0, default
    for th in thresholds:
        pred = (y_prob >= th).astype(int)
        f1 = f1_score(y_true, pred, zero_division=0)
        if f1 > best_f1:
            best_f1, best_th = f1, th
    return float(best_th)

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")

HERE      = Path(__file__).resolve().parent
PROC      = HERE.parent / "data" / "processed"
MASTER    = PROC / "master.parquet"
OUT_PQ    = PROC / "model_c_predictions.parquet"
OUT_EVAL  = PROC / "model_c_eval.txt"

TEST_START = pd.Timestamp("2022-01-01", tz="UTC")
import os as _os
_env_end = _os.environ.get("LIVE_TEST_END", "").strip()
TEST_END   = (pd.Timestamp(_env_end + " 23:30:00", tz="UTC") if _env_end
              else pd.Timestamp("2026-05-31 23:30:00", tz="UTC"))
SPIKE_TH   = 200.0
PROB_TH    = 0.5

XGB_PARAMS = dict(
    max_depth=6, learning_rate=0.05, n_estimators=300,
    tree_method="hist", n_jobs=-1, verbosity=0,
    # Phase 2G: focal loss replaces scale_pos_weight tuning.
    # `objective` is set below to the custom focal_loss_obj function.
)
# Eval threshold defaults — overridden per-month via held-out validation
# slice (last 15% of training data) when fitting.
DEFAULT_TH = 0.5
VAL_FRAC   = 0.15

t0 = time.time()
def log(m): print(f"[{(time.time()-t0):6.1f}s] {m}", flush=True)


def main():
    log("=" * 78); log("MODEL C — SBP spike classifier (leak-free)"); log("=" * 78)

    df = pd.read_parquet(MASTER, engine="fastparquet")
    f = pd.DataFrame(index=df.index)

    # Target — fixed-threshold spike label
    f["target"] = (df["sbp"] > SPIKE_TH).astype(int)

    # OPERATIONAL-SAFE FEATURES (lag-96 minimum) — see Model B for the rationale.
    # All lags ≥ 96 SPs (2 days) so they're guaranteed settled at gate closure.
    f["sbp_lag_96"]              = df["sbp"].shift(96)
    f["sbp_lag_336"]             = df["sbp"].shift(336)
    f["niv_lag_96"]              = df["niv"].shift(96)
    f["niv_lag_336"]             = df["niv"].shift(336)
    f["demand_error_lag96"]      = (df["actual_demand_mw"] - df["da_demand_forecast_mw"]).shift(96)
    f["wind_error_lag96"]        = (df["wind_actual_proxy_mw"] - df["da_wind_forecast_mw"]).shift(96)
    f["boalf_total_mw_lag96"]    = df["boalf_total_mw"].shift(96)
    f["boalf_so_count_lag96"]    = df["boalf_so_count"].shift(96)
    f["gen_pct_gas_lag96"]       = df["gen_pct_gas"].shift(96)
    f["gen_pct_wind_lag96"]      = df["gen_pct_wind"].shift(96)
    f["gen_pct_solar_lag96"]     = df["gen_pct_solar"].shift(96)
    f["gen_pct_nuclear_lag96"]   = df["gen_pct_nuclear"].shift(96)
    # Roll7d (master computes with shift(1).rolling — apply extra shift(96))
    f["sbp_roll7d_mean"]         = df["sbp_roll7d_mean"].shift(96)
    f["sbp_roll7d_std"]          = df["sbp_roll7d_std"].shift(96)
    f["niv_roll7d_mean"]         = df["niv_roll7d_mean"].shift(96)
    # Calendar
    f["hour"]                    = df["hour"]
    f["day_of_week"]             = df["day_of_week"]
    f["is_weekend"]              = df["is_weekend"]
    f["is_holiday"]              = df["is_holiday"]
    f["settlement_period"]       = df["settlement_period"]
    # Daily spike counts — shift by 2 days (was 1) for operational safety
    daily_spikes = df.groupby(df["settlement_date"])["spike_label_200"].sum()
    f["spikes_lag2d"] = pd.Series(df["settlement_date"]).map(
        lambda d: daily_spikes.shift(2).get(d, 0) if d in daily_spikes.shift(2).index else 0
    ).values
    f["spikes_lag7d"] = pd.Series(df["settlement_date"]).map(
        lambda d: daily_spikes.shift(7).get(d, 0) if d in daily_spikes.shift(7).index else 0
    ).values

    log(f"  feature matrix: {f.shape}")

    feature_cols = [c for c in f.columns if c != "target"]
    retrain_dates = pd.date_range(TEST_START, TEST_END, freq="MS", tz="UTC")
    log(f"  retrain dates: {len(retrain_dates)}")

    out_rows = []
    threshold_log = []
    for ri, rd in enumerate(retrain_dates):
        next_rd = (retrain_dates[ri + 1] if ri + 1 < len(retrain_dates)
                   else TEST_END + pd.Timedelta(days=1))
        train = f[f.index < rd].dropna(subset=feature_cols + ["target"])
        test  = f[(f.index >= rd) & (f.index < next_rd)].dropna(subset=feature_cols)
        if len(train) < 1500 or len(test) == 0:
            continue

        # ---- Split train into fit + held-out validation for threshold tuning ----
        n_val = max(int(len(train) * VAL_FRAC), 500)
        fit_data = train.iloc[:-n_val]
        val_data = train.iloc[-n_val:]
        if fit_data["target"].sum() < 5:
            # Too few positives to tune — fall back to default threshold.
            tune_th = DEFAULT_TH
        else:
            dfit = xgb.DMatrix(fit_data[feature_cols].values, label=fit_data["target"].values)
            dval = xgb.DMatrix(val_data[feature_cols].values, label=val_data["target"].values)
            booster_tune = xgb.train(
                {**{k: v for k, v in XGB_PARAMS.items() if k != "n_estimators"},
                 "disable_default_eval_metric": 1},
                dfit,
                num_boost_round=XGB_PARAMS["n_estimators"],
                obj=focal_loss_obj,
            )
            val_prob = _sigmoid(booster_tune.predict(dval))
            tune_th = find_optimal_threshold(val_data["target"].values, val_prob, DEFAULT_TH)

        # ---- Fit on full train set, predict on test ----
        dtrain = xgb.DMatrix(train[feature_cols].values, label=train["target"].values)
        dtest  = xgb.DMatrix(test[feature_cols].values)
        booster = xgb.train(
            {**{k: v for k, v in XGB_PARAMS.items() if k != "n_estimators"},
             "disable_default_eval_metric": 1},
            dtrain,
            num_boost_round=XGB_PARAMS["n_estimators"],
            obj=focal_loss_obj,
        )
        prob = _sigmoid(booster.predict(dtest))
        # Use the per-month tuned threshold (default 0.5 if tuning didn't run).
        pred = (prob >= tune_th).astype(int)
        threshold_log.append({"ym": rd.strftime("%Y-%m"), "threshold": tune_th})
        out_rows.append(pd.DataFrame({
            "actual": test["target"].values, "prob": prob, "pred": pred,
            "threshold_used": tune_th,
        }, index=test.index))
        if (ri + 1) % 6 == 0:
            log(f"  [{ri+1:>2}/{len(retrain_dates)}] {rd.strftime('%Y-%m')}: "
                f"th={tune_th:.3f}  cumulative {sum(len(x) for x in out_rows):,}")

    out = pd.concat(out_rows, axis=0)
    out.index.name = "timestamp"
    if OUT_PQ.exists():
        shutil.copy2(OUT_PQ, OUT_PQ.with_suffix(".bak.parquet"))
    out.to_parquet(OUT_PQ)
    log(f"Saved {OUT_PQ} ({len(out):,} rows)")

    y    = out["actual"].values
    yhat = out["pred"].values
    p    = out["prob"].values
    with open(OUT_EVAL, "w", encoding="utf-8") as fout:
        fout.write("=" * 70 + "\n")
        fout.write("MODEL C — SBP spike classifier (eval) — Phase 2G focal loss + threshold tuning\n")
        fout.write("=" * 70 + "\n")
        fout.write(f"Total predictions: {len(out):,}\n")
        fout.write(f"Spike rate (actual): {y.mean()*100:.2f}%\n")
        fout.write(f"AUC-ROC:    {roc_auc_score(y, p):.4f}\n")
        fout.write(f"AUC-PR:     {average_precision_score(y, p):.4f}\n")
        fout.write(f"Precision:  {precision_score(y, yhat, zero_division=0):.4f}\n")
        fout.write(f"Recall:     {recall_score(y, yhat, zero_division=0):.4f}\n")
        fout.write(f"F1:         {f1_score(y, yhat, zero_division=0):.4f}\n\n")
        fout.write("Per-year:\n")
        out["year"] = out.index.year
        for yr in sorted(out["year"].unique()):
            grp = out[out["year"] == yr]
            if grp["actual"].sum() == 0:
                fout.write(f"  {yr}: no spikes (N={len(grp):,})\n"); continue
            f1 = f1_score(grp["actual"], grp["pred"], zero_division=0)
            auc = roc_auc_score(grp["actual"], grp["prob"]) if grp["actual"].nunique() > 1 else float("nan")
            fout.write(f"  {yr}: F1={f1:.4f}  AUC={auc:.4f}  N={len(grp):,}  spikes={int(grp['actual'].sum())}\n")
        fout.write("\nPer-month tuned thresholds:\n")
        for row in threshold_log:
            fout.write(f"  {row['ym']}: threshold = {row['threshold']:.3f}\n")
    log(f"Eval: {OUT_EVAL}")


if __name__ == "__main__":
    main()
