"""
Clean Pipeline — Stage 3b: Model A LightGBM 'extras' day-ahead forecaster.

Same walk-forward schedule as LEAR, but with a per-period LightGBM regressor
on an EXTENDED feature set: canonical LEAR features PLUS Brent crude and
UKA carbon as macro/sentiment proxies.

UNTUNED hyperparameters are used by canonical design — the tuned variant
was rejected in the dissertation gate because it regressed on 2022 MAE.

Input:  data/processed/master.parquet
Output: data/processed/model_a_lgbm_extras_predictions.parquet
        data/processed/model_a_lgbm_extras_eval.txt
"""

import sys, time, shutil, warnings
from pathlib import Path
import pandas as pd
import numpy as np
import lightgbm as lgb

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")

HERE      = Path(__file__).resolve().parent
PROC      = HERE.parent / "data" / "processed"
MASTER    = PROC / "master.parquet"
OUT_PQ    = PROC / "model_a_lgbm_extras_predictions.parquet"
OUT_EVAL  = PROC / "model_a_lgbm_extras_eval.txt"

TEST_START = pd.Timestamp("2022-01-01")
import os as _os
_env_end = _os.environ.get("LIVE_TEST_END", "").strip()
TEST_END   = pd.Timestamp(_env_end) if _env_end else pd.Timestamp("2026-05-31")
MIN_TRAIN_DAYS = 30

# UNTUNED LightGBM — canonical (tuned variant rejected per dissertation gate)
LGBM_PARAMS = dict(
    n_estimators=300, learning_rate=0.05, num_leaves=31, max_depth=-1,
    min_child_samples=20, verbose=-1,
)

t0 = time.time()
def log(m): print(f"[{(time.time()-t0):6.1f}s] {m}", flush=True)


def build_features(price_wide, demand_wide, wind_wide, day_scalars, p):
    last_col = 48 if 48 in price_wide.columns else price_wide.columns[-1]
    yest = price_wide.shift(1)
    feats = pd.DataFrame({
        "lag1":         price_wide[p].shift(1) if p in price_wide.columns else np.nan,
        "lag2":         price_wide[p].shift(2) if p in price_wide.columns else np.nan,
        "lag7":         price_wide[p].shift(7) if p in price_wide.columns else np.nan,
        "last_p_yest":  price_wide[last_col].shift(1),
        "yest_min":     yest.min(axis=1,  skipna=True),
        "yest_max":     yest.max(axis=1,  skipna=True),
        "yest_mean":    yest.mean(axis=1, skipna=True),
        "dem_today":    demand_wide[p] if p in demand_wide.columns else np.nan,
        "wnd_today":    wind_wide[p]   if p in wind_wide.columns   else np.nan,
        "gas_yest":     day_scalars["gas"].shift(1),
        "brent_yest":   day_scalars["brent"].shift(1),
        "uka_yest":     day_scalars["uka"].shift(1),
        "dow_mon": (day_scalars["day_of_week"] == 0).astype(int),
        "dow_tue": (day_scalars["day_of_week"] == 1).astype(int),
        "dow_wed": (day_scalars["day_of_week"] == 2).astype(int),
        "dow_thu": (day_scalars["day_of_week"] == 3).astype(int),
        "dow_fri": (day_scalars["day_of_week"] == 4).astype(int),
        "dow_sat": (day_scalars["day_of_week"] == 5).astype(int),
        "is_holiday": day_scalars["is_holiday"],
    })
    feats["target"] = price_wide[p] if p in price_wide.columns else pd.Series(dtype=float)
    return feats


def main():
    log("=" * 78); log("MODEL A LGBM (extras) — walk-forward"); log("=" * 78)

    log("Loading master parquet…")
    try:    df = pd.read_parquet(MASTER)
    except: df = pd.read_parquet(MASTER, engine="fastparquet")
    log(f"  {len(df):,} rows × {len(df.columns)} cols")

    df["date"] = pd.to_datetime(df["settlement_date"])
    price_wide  = df.pivot_table(index="date", columns="settlement_period", values="da_price")
    demand_wide = df.pivot_table(index="date", columns="settlement_period", values="da_demand_forecast_mw")
    wind_wide   = df.pivot_table(index="date", columns="settlement_period", values="da_wind_forecast_mw")
    day_scalars = df.groupby("date").agg(
        gas         = ("gas_nbp_p_therm",   "first"),
        brent       = ("brent_usd",         "first"),
        uka         = ("uka_eur",           "first"),
        is_holiday  = ("is_holiday",        "first"),
        day_of_week = ("day_of_week",       "first"),
    )
    # Brent / UKA may have weekend gaps — ffill (canonical)
    day_scalars[["brent", "uka"]] = day_scalars[["brent", "uka"]].ffill()
    log(f"  scalars non-null: gas={day_scalars['gas'].notna().sum()}, "
        f"brent={day_scalars['brent'].notna().sum()}, uka={day_scalars['uka'].notna().sum()}")

    retrain_dates = pd.date_range(TEST_START, TEST_END, freq="MS")
    log(f"Walk-forward retrains: {len(retrain_dates)}")

    feats_cache = {p: build_features(price_wide, demand_wide, wind_wide, day_scalars, p)
                   for p in range(1, 49)}

    all_preds = []
    for ri, rd in enumerate(retrain_dates):
        next_rd = (retrain_dates[ri + 1] if ri + 1 < len(retrain_dates)
                   else TEST_END + pd.Timedelta(days=1))
        period_models = {}
        for p in range(1, 49):
            f = feats_cache[p]
            train = f[f.index < rd].dropna()
            if len(train) < MIN_TRAIN_DAYS:
                continue
            X = train.drop(columns=["target"])
            y = train["target"]
            try:
                m = lgb.LGBMRegressor(**LGBM_PARAMS)
                m.fit(X, y)
                period_models[p] = (m, X.columns.tolist())
            except Exception:
                continue

        predict_days = day_scalars.index[(day_scalars.index >= rd) & (day_scalars.index < next_rd)]
        n_pred = 0
        for d in predict_days:
            for p in range(1, 49):
                if p not in period_models:
                    continue
                f = feats_cache[p]
                if d not in f.index:
                    continue
                row = f.loc[d]
                if row.drop("target").isna().any():
                    continue
                actual = row["target"]
                Xrow = row.drop("target").values.reshape(1, -1)
                m, _ = period_models[p]
                pred = float(m.predict(Xrow)[0])
                all_preds.append({"date": d, "period": p, "actual": actual, "predicted": pred})
                n_pred += 1
        log(f"  [{ri+1:>2}/{len(retrain_dates)}] {rd.strftime('%Y-%m')}: predicted={n_pred:>5}  total={len(all_preds):,}")

    preds = pd.DataFrame(all_preds)
    if OUT_PQ.exists():
        shutil.copy2(OUT_PQ, OUT_PQ.with_suffix(".bak.parquet"))
    preds.to_parquet(OUT_PQ)
    log(f"Saved {OUT_PQ} ({len(preds):,} rows)")

    preds["err"]  = (preds["actual"] - preds["predicted"]).abs()
    preds["year"] = pd.to_datetime(preds["date"]).dt.year
    with open(OUT_EVAL, "w", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write("MODEL A LGBM EXTRAS — eval (UNTUNED, canonical)\n")
        f.write("=" * 70 + "\n")
        f.write(f"Total predictions: {len(preds):,}\n")
        f.write(f"Overall MAE:  {preds['err'].mean():.2f}\n")
        f.write(f"Overall RMSE: {np.sqrt((preds['err']**2).mean()):.2f}\n\n")
        f.write("Per-year MAE:\n")
        for yr, grp in preds.groupby("year"):
            f.write(f"  {yr}: MAE {grp['err'].mean():.2f}  N={len(grp):,}\n")
    log(f"Eval: {OUT_EVAL}")


if __name__ == "__main__":
    main()
