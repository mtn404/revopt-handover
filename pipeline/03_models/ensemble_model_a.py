"""
Clean Pipeline — Stage 3c: Model A walk-forward ensemble.

Combines LEAR + LightGBM 'extras' predictions under a forecast-combination
scheme (Nowotarski et al., 2014). At each monthly boundary, the convex
weight w_LEAR ∈ [0, 1] that minimises MAE on the PRECEDING 3-month rolling
validation window is selected from a 21-point grid, then applied to the
NEXT month's out-of-sample predictions.

Walk-forward — never any future information leaks into the weight.

Inputs:  data/processed/model_a_lear_predictions.parquet
         data/processed/model_a_lgbm_extras_predictions.parquet
Output:  data/processed/model_a_ensemble_predictions.parquet
         data/processed/model_a_ensemble_eval.txt
"""

import sys, time, shutil
from pathlib import Path
import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

HERE      = Path(__file__).resolve().parent
PROC      = HERE.parent / "data" / "processed"
LEAR_PQ   = PROC / "model_a_lear_predictions.parquet"
LGBM_PQ   = PROC / "model_a_lgbm_extras_predictions.parquet"
OUT_PQ    = PROC / "model_a_ensemble_predictions.parquet"
OUT_EVAL  = PROC / "model_a_ensemble_eval.txt"

VAL_MONTHS  = 3
WEIGHT_GRID = np.arange(0.0, 1.001, 0.05)   # 21 candidates
DEFAULT_W   = 0.5

t0 = time.time()
def log(m): print(f"[{(time.time()-t0):6.1f}s] {m}", flush=True)


def main():
    log("=" * 78); log("MODEL A ENSEMBLE — walk-forward weight selection"); log("=" * 78)

    lear = pd.read_parquet(LEAR_PQ)
    lgbm = pd.read_parquet(LGBM_PQ)
    log(f"  LEAR rows: {len(lear):,}")
    log(f"  LGBM rows: {len(lgbm):,}")

    merged = lear.merge(lgbm, on=["date", "period"], suffixes=("_lear", "_lgbm"))
    # Drop rows only where BOTH PREDICTIONS are missing — keep forward-day rows
    # where actual hasn't settled yet but predictions exist. This enables the
    # ensemble to produce forecasts for tomorrow.
    merged = merged.dropna(subset=["predicted_lear", "predicted_lgbm"])
    merged["date"] = pd.to_datetime(merged["date"])
    merged["ym"]   = merged["date"].dt.to_period("M")
    merged = merged.sort_values(["date", "period"]).reset_index(drop=True)
    log(f"  Aligned rows: {len(merged):,} "
        f"(with-actuals: {merged['actual_lear'].notna().sum():,}, "
        f"forward-only: {merged['actual_lear'].isna().sum():,})")

    months = sorted(merged["ym"].unique())
    log(f"  Test months: {months[0]} → {months[-1]} ({len(months)})")

    def mae(y, yhat):
        return float(np.mean(np.abs(y - yhat)))

    ensemble_rows = []
    weight_log = []
    for i, m in enumerate(months):
        test = merged[merged["ym"] == m]
        if test.empty: continue
        if i < VAL_MONTHS:
            w = DEFAULT_W
        else:
            val = merged[merged["ym"].isin(months[i - VAL_MONTHS:i])]
            # Restrict validation to rows where actual is known; forward rows
            # in the most recent (partial) validation month are excluded.
            val = val[val["actual_lear"].notna()]
            if val.empty:
                w = DEFAULT_W
            else:
                y = val["actual_lear"].values
                l = val["predicted_lear"].values
                g = val["predicted_lgbm"].values
                best_w, best_mae = DEFAULT_W, float("inf")
                for cand in WEIGHT_GRID:
                    err = mae(y, cand * l + (1 - cand) * g)
                    if err < best_mae:
                        best_mae, best_w = err, cand
                w = best_w
        weight_log.append({"ym": str(m), "w_lear": w})
        ens = w * test["predicted_lear"] + (1 - w) * test["predicted_lgbm"]
        ensemble_rows.append(pd.DataFrame({
            "date": test["date"], "period": test["period"],
            "actual": test["actual_lear"], "predicted": ens, "w_lear": w,
        }))
        if (i + 1) % 6 == 0:
            log(f"  [{i+1:>2}/{len(months)}] {m}: w_lear={w:.2f}")

    out = pd.concat(ensemble_rows, ignore_index=True)
    if OUT_PQ.exists():
        shutil.copy2(OUT_PQ, OUT_PQ.with_suffix(".bak.parquet"))
    out.to_parquet(OUT_PQ)
    log(f"Saved {OUT_PQ} ({len(out):,} rows)")

    out["err"]  = (out["actual"] - out["predicted"]).abs()
    out["year"] = pd.to_datetime(out["date"]).dt.year
    lear["err"] = (lear["actual"] - lear["predicted"]).abs()
    lear["year"] = pd.to_datetime(lear["date"]).dt.year

    with open(OUT_EVAL, "w", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write("MODEL A ENSEMBLE — eval\n")
        f.write("=" * 70 + "\n")
        f.write(f"Overall MAE  (LEAR):     {lear['err'].mean():.2f}\n")
        f.write(f"Overall MAE  (ENSEMBLE): {out['err'].mean():.2f}\n")
        impr = (lear['err'].mean() - out['err'].mean()) / lear['err'].mean() * 100
        f.write(f"MAE improvement: {impr:+.2f}%\n\n")
        f.write("Per-year MAE:\n")
        for yr in sorted(set(out["year"]).union(lear["year"])):
            l_mae = lear[lear["year"] == yr]["err"].mean()
            e_mae = out [out["year"]  == yr]["err"].mean()
            f.write(f"  {yr}:  LEAR={l_mae:.2f}  ENSEMBLE={e_mae:.2f}  N={(out['year']==yr).sum():,}\n")
        f.write("\nWalk-forward weight trajectory (w_LEAR):\n")
        for row in weight_log:
            f.write(f"  {row['ym']}: w_LEAR = {row['w_lear']:.2f}\n")
    log(f"Eval: {OUT_EVAL}")


if __name__ == "__main__":
    main()
