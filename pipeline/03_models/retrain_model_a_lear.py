"""
Clean Pipeline — Stage 3a: Model A LEAR (Lasso) day-ahead price forecaster.

Walk-forward train: at each monthly boundary, train 48 per-period Lasso models
on data strictly BEFORE the boundary; predict the period AFTER it.

CRITICAL: explicit handling of feature gaps. Previously, the canonical script
silently dropped any day-period where ANY feature was NaN. That caused the
"Wednesday gap" we found — a Tuesday with one missing SP cascaded into a
Wednesday with zero predictions because yest_min/max/mean were NaN.

This version:
  - Computes yest_* features with min_periods so a single missing SP doesn't
    destroy the whole day's predictions.
  - Logs every skipped period (so you can audit later).

Input:  data/processed/master.parquet
Output: data/processed/model_a_lear_predictions.parquet
        data/processed/model_a_lear_eval.txt
"""

import sys, time, shutil
from pathlib import Path
import pandas as pd
import numpy as np
import warnings
from sklearn.linear_model import LassoCV
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")

HERE       = Path(__file__).resolve().parent
PROCESSED  = HERE.parent / "data" / "processed"
MASTER     = PROCESSED / "master.parquet"
OUT_PQ     = PROCESSED / "model_a_lear_predictions.parquet"
OUT_EVAL   = PROCESSED / "model_a_lear_eval.txt"

TEST_START = pd.Timestamp("2022-01-01")
# TEST_END overridable via env var for live operation. Default = dissertation freeze.
import os as _os
_env_end = _os.environ.get("LIVE_TEST_END", "").strip()
TEST_END   = pd.Timestamp(_env_end) if _env_end else pd.Timestamp("2026-05-31")
ALPHAS     = np.logspace(-3, 1, 20)
CV_SPLITS  = 3
MIN_TRAIN_DAYS = 30

t0 = time.time()
def log(m): print(f"[{(time.time()-t0):6.1f}s] {m}", flush=True)


def build_features(price_wide, demand_wide, wind_wide, day_scalars, p):
    """
    Build per-period feature frame for settlement period p.
    Uses yest_min/max/mean computed with min_periods=24 (half-day) so
    a single-SP missing day doesn't destroy the whole NEXT day's features.
    """
    target = price_wide[p] if p in price_wide.columns else pd.Series(dtype=float)

    lag1 = price_wide[p].shift(1) if p in price_wide.columns else pd.Series(dtype=float)
    lag2 = price_wide[p].shift(2) if p in price_wide.columns else pd.Series(dtype=float)
    lag7 = price_wide[p].shift(7) if p in price_wide.columns else pd.Series(dtype=float)

    # last-period yesterday (use SP 48 if available, else last column)
    last_col = 48 if 48 in price_wide.columns else price_wide.columns[-1]
    last_p_yest = price_wide[last_col].shift(1)

    # Yesterday's stats — TOLERATE up to 24 NaN out of 48 SPs (half-day missing OK)
    yest = price_wide.shift(1)
    yest_min  = yest.min(axis=1,  skipna=True)
    yest_max  = yest.max(axis=1,  skipna=True)
    yest_mean = yest.mean(axis=1, skipna=True)

    feats = pd.DataFrame({
        "lag1": lag1, "lag2": lag2, "lag7": lag7,
        "last_p_yest": last_p_yest,
        "yest_min":  yest_min, "yest_max": yest_max, "yest_mean": yest_mean,
        "dem_today": demand_wide[p] if p in demand_wide.columns else np.nan,
        "wnd_today":   wind_wide[p] if p in wind_wide.columns   else np.nan,
        "gas_yest":    day_scalars["gas"].shift(1),
        "dow_mon":     (day_scalars["day_of_week"] == 0).astype(int),
        "dow_tue":     (day_scalars["day_of_week"] == 1).astype(int),
        "dow_wed":     (day_scalars["day_of_week"] == 2).astype(int),
        "dow_thu":     (day_scalars["day_of_week"] == 3).astype(int),
        "dow_fri":     (day_scalars["day_of_week"] == 4).astype(int),
        "dow_sat":     (day_scalars["day_of_week"] == 5).astype(int),
        "is_holiday":  day_scalars["is_holiday"],
    })
    feats["target"] = target
    return feats


def main():
    log("=" * 78); log("MODEL A LEAR — walk-forward"); log("=" * 78)

    log("Loading master parquet…")
    try:    df = pd.read_parquet(MASTER)
    except: df = pd.read_parquet(MASTER, engine="fastparquet")
    log(f"  {len(df):,} rows × {len(df.columns)} cols, range {df.index.min()} → {df.index.max()}")

    # Daily pivots
    df["date"] = pd.to_datetime(df["settlement_date"])
    price_wide  = df.pivot_table(index="date", columns="settlement_period", values="da_price")
    demand_wide = df.pivot_table(index="date", columns="settlement_period", values="da_demand_forecast_mw")
    wind_wide   = df.pivot_table(index="date", columns="settlement_period", values="da_wind_forecast_mw")
    day_scalars = df.groupby("date").agg(
        gas         = ("gas_nbp_p_therm",  "first"),
        is_holiday  = ("is_holiday",       "first"),
        day_of_week = ("day_of_week",      "first"),
    )
    log(f"  price_wide {price_wide.shape}, gas non-null: {day_scalars['gas'].notna().sum()}/{len(day_scalars)}")

    retrain_dates = pd.date_range(TEST_START, TEST_END, freq="MS")
    log(f"Walk-forward retrains: {len(retrain_dates)} starting {retrain_dates[0].date()} ending {retrain_dates[-1].date()}")

    all_preds = []
    skip_log  = []

    feats_cache = {p: build_features(price_wide, demand_wide, wind_wide, day_scalars, p)
                   for p in range(1, 49)}
    log("  features cached for all 48 SPs")

    for ri, rd in enumerate(retrain_dates):
        next_rd = (retrain_dates[ri + 1] if ri + 1 < len(retrain_dates)
                   else TEST_END + pd.Timedelta(days=1))

        # Train 48 per-period Lasso models on data BEFORE retrain date
        period_models = {}
        for p in range(1, 49):
            f = feats_cache[p]
            train = f[f.index < rd].dropna()
            if len(train) < MIN_TRAIN_DAYS:
                continue
            X = train.drop(columns=["target"])
            y = train["target"]
            scaler = StandardScaler()
            Xs = scaler.fit_transform(X)
            try:
                model = LassoCV(cv=TimeSeriesSplit(n_splits=CV_SPLITS),
                                max_iter=2000, alphas=ALPHAS, n_jobs=-1)
                model.fit(Xs, y)
                period_models[p] = (model, scaler, X.columns.tolist())
            except Exception:
                continue

        predict_days = day_scalars.index[
            (day_scalars.index >= rd) & (day_scalars.index < next_rd)
        ]
        n_pred = n_skip = 0
        for d in predict_days:
            for p in range(1, 49):
                if p not in period_models:
                    n_skip += 1; continue
                f = feats_cache[p]
                if d not in f.index:
                    n_skip += 1; continue
                row = f.loc[d]
                # Drop only this period if ITS feature row has NaN — log it
                if row.drop("target").isna().any():
                    skip_log.append((d, p, list(row.drop("target").index[row.drop("target").isna()])))
                    n_skip += 1; continue
                actual = row["target"]
                X = row.drop("target").values.reshape(1, -1)
                model, scaler, _ = period_models[p]
                pred = float(model.predict(scaler.transform(X))[0])
                all_preds.append({"date": d, "period": p, "actual": actual, "predicted": pred})
                n_pred += 1
        log(f"  [{ri+1:>2}/{len(retrain_dates)}] {rd.strftime('%Y-%m')}: "
            f"predicted={n_pred:>5} skipped={n_skip:>5}  total={len(all_preds):,}")

    preds = pd.DataFrame(all_preds)
    log(f"Total predictions: {len(preds):,}")

    # Backup existing OUT and write
    if OUT_PQ.exists():
        shutil.copy2(OUT_PQ, OUT_PQ.with_suffix(".bak.parquet"))
    preds.to_parquet(OUT_PQ)
    log(f"Saved {OUT_PQ}")

    # Eval text
    preds["err"] = (preds["actual"] - preds["predicted"]).abs()
    preds["year"] = pd.to_datetime(preds["date"]).dt.year
    with open(OUT_EVAL, "w", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write("MODEL A LEAR — eval\n")
        f.write("=" * 70 + "\n")
        f.write(f"Aligned predictions: {len(preds):,}\n")
        f.write(f"Overall MAE:  {preds['err'].mean():.2f}\n")
        f.write(f"Overall RMSE: {np.sqrt((preds['err']**2).mean()):.2f}\n\n")
        f.write("Per-year MAE:\n")
        for yr, grp in preds.groupby("year"):
            f.write(f"  {yr}: MAE {grp['err'].mean():.2f}  N={len(grp):,}\n")
        f.write(f"\nSkipped period-day pairs: {len(skip_log):,}\n")
        if skip_log:
            f.write("Top 10 missing-feature columns across skipped predictions:\n")
            from collections import Counter
            cnt = Counter()
            for d, p, cols in skip_log:
                cnt.update(cols)
            for col, n in cnt.most_common(10):
                f.write(f"  {col}: {n:,}\n")
    log(f"Eval: {OUT_EVAL}")

    # Per-day SP coverage
    pred_per_day = preds.groupby("date")["period"].nunique()
    log("Per-day SP coverage:")
    log(f"  Fully-covered days (48 SPs): {(pred_per_day == 48).sum():,}")
    log(f"  Days with any prediction:    {len(pred_per_day):,}")


if __name__ == "__main__":
    main()
