"""
Clean Pipeline — Stage 2: Build the master half-hourly dataframe.

Input:  data/raw/*.csv  (from 01_data_pulls/)
        data/proprietary/Spectron_*.xlsx, DA_Prices.xlsx, uka_daily_interpolated.csv
        data/raw/yahoo_brent.csv

Output: data/processed/master.parquet
        data/processed/master_coverage.txt   (per-day, per-column coverage report)

Methodology — IMPORTANT INVARIANTS:
  - Index is UTC half-hourly timestamps from 2021-01-01T00:00Z to 2026-05-31T23:30Z.
  - Every column is joined left → master grid, so missing data shows as NaN (never silently dropped).
  - settlement_date is the UK-local DATE (so a row at 2025-03-30T23:30Z BST is in 2025-03-31).
  - All resamples explicitly use 30min frequency. No implicit aggregation.
  - Generation mix accepts BOTH:
      (a) long format (one row per fuel × time), columns: from, to, fuel, perc
      (b) nested JSON 'generationmix' column. If nested, it is flattened.
  - Spectron NBP gas is written to the column 'gas_nbp_p_therm' (UK NBP DA mid in pence/therm).
    The FRED European TTF gas is kept under 'gas_eu_usd_mmbtu_proxy'.

  - NO same-day DA features are computed (NESO EAC clears D-1 09:00, EPEX DA clears D-1 11:00).
    Lag-1, lag-7, etc. are always computed AFTER the join so they shift cleanly.

  - Day-of-week / month / holiday / EFA block are computed from the UK-local clock,
    not UTC, so the EFA boundaries align with NESO auction blocks.
"""

import sys, gc
from datetime import datetime
from pathlib import Path
import pandas as pd
import numpy as np
import holidays

sys.stdout.reconfigure(encoding="utf-8")

# ---- Paths ----
HERE          = Path(__file__).resolve().parent
RAW           = HERE.parent / "data" / "raw"
PROPRIETARY   = HERE.parent / "data" / "proprietary"
PROCESSED     = HERE.parent / "data" / "processed"
PROCESSED.mkdir(parents=True, exist_ok=True)

# ---- Window ----
START = pd.Timestamp("2021-01-01 00:00:00", tz="UTC")
import os as _os
_env_end = _os.environ.get("LIVE_TEST_END", "").strip()
END   = (pd.Timestamp(_env_end + " 23:30:00", tz="UTC") if _env_end
         else pd.Timestamp("2026-05-31 23:30:00", tz="UTC"))

# ---- Logging ----
import time
t0 = time.time()
def log(msg): print(f"[{(time.time()-t0):6.1f}s] {msg}", flush=True)

# ---- Coverage tracker ----
coverage_report = []
def report(col, df, total):
    nn = df[col].notna().sum() if col in df.columns else 0
    pct = nn / total * 100 if total else 0
    line = f"  {col:<32} {nn:>8,} / {total:,}  ({pct:>5.1f}%)"
    coverage_report.append(line)
    log(line)


def main():
    log("=" * 78)
    log("BUILD MASTER")
    log("=" * 78)

    # ============================================================
    # Phase 1 — half-hourly index
    # ============================================================
    log(f"Phase 1: Building 30-min UTC grid {START} → {END}")
    idx = pd.date_range(START, END, freq="30min", tz="UTC")
    df = pd.DataFrame(index=idx)
    df.index.name = "timestamp"
    TOTAL = len(df)
    log(f"  rows: {TOTAL:,}")
    # UK-local clock and settlement period
    df["uk_local"] = df.index.tz_convert("Europe/London")
    df["settlement_date"]   = df["uk_local"].dt.date
    df["settlement_period"] = (df["uk_local"].dt.hour * 2 +
                               df["uk_local"].dt.minute // 30 + 1)

    # ============================================================
    # Phase 2 — BMRS day-ahead prices (MID)
    # ============================================================
    log("Phase 2: BMRS day-ahead prices (MID)")
    mid = pd.read_csv(RAW / "bmrs_da_prices.csv")
    # format="mixed": some pulls return tz-aware, some tz-naive ISO. mixed is robust to both.
    mid["startTime"] = pd.to_datetime(mid["startTime"], utc=True, format="mixed")
    # Volume-weighted average per startTime (multiple bids per settlement period)
    def vw_avg(g):
        if g["volume"].sum() > 0:
            return (g["price"] * g["volume"]).sum() / g["volume"].sum()
        return g["price"].mean()
    mid_agg = (mid.groupby("startTime", group_keys=False)
                  .apply(lambda g: pd.Series({
                      "mid_price":  vw_avg(g),
                      "mid_volume": g["volume"].sum(),
                  })))
    df = df.join(mid_agg, how="left")
    df["da_price"] = df["mid_price"]            # canonical alias
    report("da_price", df, TOTAL)
    del mid, mid_agg; gc.collect()

    # ============================================================
    # Phase 3 — BMRS system prices
    # ============================================================
    log("Phase 3: BMRS system prices (SBP / SSP / NIV)")
    sp = pd.read_csv(RAW / "bmrs_system_prices.csv",
                     usecols=["startTime", "systemSellPrice", "systemBuyPrice",
                              "netImbalanceVolume", "totalAcceptedOfferVolume",
                              "totalAcceptedBidVolume"])
    sp["startTime"] = pd.to_datetime(sp["startTime"], utc=True, format="mixed")
    sp = sp.rename(columns={
        "systemSellPrice": "ssp", "systemBuyPrice": "sbp",
        "netImbalanceVolume": "niv",
        "totalAcceptedOfferVolume": "total_offer_vol",
        "totalAcceptedBidVolume":   "total_bid_vol",
    }).set_index("startTime")
    sp = sp[~sp.index.duplicated(keep="first")]
    df = df.join(sp, how="left")
    report("sbp", df, TOTAL)
    report("niv", df, TOTAL)
    del sp; gc.collect()

    # ============================================================
    # Phase 3b — BMRS interconnector outturn (Phase 2 feature)
    #
    # Aggregated to a single ic_net_flow_mw per SP (sum across all ICs;
    # positive = net imports, system-cap signal; negative = net exports,
    # system-tighten signal). Keep ic_count as the number of ICs reporting
    # so coverage can be diagnosed.
    # ============================================================
    log("Phase 3b: BMRS interconnector outturn")
    ic_path = RAW / "bmrs_interconnectors.csv"
    if ic_path.exists():
        ic = pd.read_csv(ic_path, usecols=["startTime", "interconnectorName", "generation"])
        ic["startTime"] = pd.to_datetime(ic["startTime"], utc=True, format="mixed")
        ic = ic.set_index("startTime").sort_index()
        ic_agg = ic.groupby(level=0).agg(
            ic_net_flow_mw=("generation", "sum"),
            ic_count=("interconnectorName", "nunique"),
        )
        df = df.join(ic_agg, how="left")
        report("ic_net_flow_mw", df, TOTAL)
        del ic, ic_agg; gc.collect()
    else:
        log(f"  {ic_path.name} not found — skipping (run pull_phase2_features.py first)")

    # ============================================================
    # Phase 3c — BMRS system prices extended fields (Phase 2 feature)
    #
    # Same endpoint we read in Phase 3 (system-prices), but adds the
    # BSAD-derived adjustments + reserve scarcity premium + accepted
    # volumes that weren't being persisted before. These are the
    # marginal cashout drivers — strong SBP-spike predictors.
    # ============================================================
    log("Phase 3c: BMRS system prices extended (BSAD adjustments + scarcity)")
    sp2_path = RAW / "bmrs_system_prices_v2.csv"
    if sp2_path.exists():
        keep = [
            "startTime",
            "sellPriceAdjustment", "buyPriceAdjustment",
            "reserveScarcityPrice", "priceDerivationCode", "bsadDefaulted",
        ]
        sp2 = pd.read_csv(sp2_path, usecols=lambda c: c in keep)
        sp2["startTime"] = pd.to_datetime(sp2["startTime"], utc=True, format="mixed")
        sp2 = sp2.set_index("startTime")
        sp2 = sp2[~sp2.index.duplicated(keep="first")]
        # Numerical fields: float; flag fields: nullable bool/int
        for c in ["sellPriceAdjustment", "buyPriceAdjustment", "reserveScarcityPrice"]:
            if c in sp2.columns:
                sp2[c] = pd.to_numeric(sp2[c], errors="coerce")
        df = df.join(sp2, how="left")
        for c in ["sellPriceAdjustment", "buyPriceAdjustment", "reserveScarcityPrice"]:
            if c in df.columns:
                report(c, df, TOTAL)
        del sp2; gc.collect()
    else:
        log(f"  {sp2_path.name} not found — skipping (run pull_phase2_features.py first)")

    # ============================================================
    # Phase 4 — Demand
    # ============================================================
    log("Phase 4: BMRS demand outturn")
    dem = pd.read_csv(RAW / "bmrs_demand_outturn.csv", usecols=["startTime", "demand"])
    dem["startTime"] = pd.to_datetime(dem["startTime"], utc=True, format="mixed")
    dem = dem.groupby("startTime").mean()
    df = df.join(dem.rename(columns={"demand": "actual_demand_mw"}), how="left")
    report("actual_demand_mw", df, TOTAL)
    del dem; gc.collect()

    # ============================================================
    # Phase 4b — GAP-FILL from existing patched master (same Elexon Insights
    # source, earlier API snapshot). Elexon's API has lost coverage on ~290
    # days that the earlier snapshot still has; this restores them.
    # See validate_pulls.py output for gap counts. Same canonical source —
    # not introducing a different price feed.
    # ============================================================
    EXISTING_MASTER = Path(r"C:/Users/Amgomed/Desktop/UCL Study/Utilidex/Data/processed/master_dataframe_v4.bak.parquet")
    if EXISTING_MASTER.exists():
        log("Phase 4b: Gap-fill DA price + demand from existing patched master")
        try:    existing = pd.read_parquet(EXISTING_MASTER)
        except: existing = pd.read_parquet(EXISTING_MASTER, engine="fastparquet")
        # Align indexes
        existing = existing[~existing.index.duplicated(keep="first")]
        for col, exists_col in [("da_price", "da_price"),
                                ("mid_price", "mid_price"),
                                ("actual_demand_mw", "actual_demand_mw"),
                                ("sbp", "sbp"),
                                ("niv", "niv"),
                                ("ssp", "ssp")]:
            if exists_col in existing.columns and col in df.columns:
                ex_series = existing[exists_col].reindex(df.index)
                before_nn = df[col].notna().sum()
                df[col] = df[col].fillna(ex_series)
                after_nn = df[col].notna().sum()
                filled = after_nn - before_nn
                if filled > 0:
                    log(f"  {col:<20} filled {filled:>6,} NaN rows from existing master  "
                        f"({before_nn:,} → {after_nn:,})")
        del existing; gc.collect()
    else:
        log("Phase 4b: SKIPPED — existing patched master not found")
    report("da_price (after gap-fill)", df, TOTAL)
    report("actual_demand_mw (after gap-fill)", df, TOTAL)
    report("sbp (after gap-fill)", df, TOTAL)

    # ============================================================
    # Phase 5 — Generation mix (Carbon Intensity)
    # ============================================================
    log("Phase 5: Carbon Intensity generation mix")
    gm = pd.read_csv(RAW / "ci_generation_mix.csv")
    # Accept either long (from/to/fuel/perc) or nested JSON 'generationmix' format
    if "generationmix" in gm.columns and "fuel" not in gm.columns:
        log("  detected nested JSON format — flattening")
        import ast
        rows = []
        for _, r in gm.iterrows():
            try:
                items = ast.literal_eval(r["generationmix"]) if isinstance(r["generationmix"], str) else r["generationmix"]
            except Exception:
                continue
            for item in items:
                rows.append({"from": r["from"], "to": r["to"],
                             "fuel": item["fuel"], "perc": item["perc"]})
        gm = pd.DataFrame(rows)
    gm["from"]  = pd.to_datetime(gm["from"], utc=True, format="mixed")
    gm_wide = gm.pivot_table(index="from", columns="fuel", values="perc", aggfunc="first")
    gm_wide.columns = [f"gen_pct_{c}" for c in gm_wide.columns]
    df = df.join(gm_wide, how="left")
    for c in ["gen_pct_gas", "gen_pct_wind", "gen_pct_solar", "gen_pct_nuclear",
              "gen_pct_biomass", "gen_pct_coal", "gen_pct_hydro", "gen_pct_imports",
              "gen_pct_other"]:
        report(c, df, TOTAL)
    del gm, gm_wide; gc.collect()

    # ============================================================
    # Phase 6 — BOALF aggregation
    #
    # Two source paths:
    #   (a) Pre-aggregated boalf_aggregates.csv (~17 MB)  — preferred in live
    #       mode. Produced by pipeline/01_ingestion/aggregate_boalf.py.
    #   (b) Raw bmrs_boalf.csv (~2.4 GB) — used locally during initial pipeline
    #       construction. Chunked-aggregation, same output schema.
    # Either source produces the same 6 boalf_* columns joined onto df.
    # ============================================================
    log("Phase 6: BMRS BOALF")
    boalf_agg_path = RAW / "boalf_aggregates.csv"
    boalf_raw_path = RAW / "bmrs_boalf.csv"
    boalf = None

    if boalf_agg_path.exists():
        log("  using pre-aggregated boalf_aggregates.csv")
        ba = pd.read_csv(boalf_agg_path, parse_dates=["bucket"])
        if ba["bucket"].dt.tz is None:
            ba["bucket"] = ba["bucket"].dt.tz_localize("UTC")
        boalf = ba.set_index("bucket")
        log(f"  {len(boalf):,} pre-aggregated half-hour rows")
    elif boalf_raw_path.exists():
        log("  using raw bmrs_boalf.csv (chunked aggregation)")
        boalf_chunks = []
        for chunk in pd.read_csv(boalf_raw_path,
                                 usecols=["timeFrom", "timeTo", "levelFrom", "levelTo", "soFlag"],
                                 chunksize=500_000):
            chunk["timeFrom"] = pd.to_datetime(chunk["timeFrom"], utc=True)
            chunk["delta_mw"]  = (chunk["levelTo"] - chunk["levelFrom"]).abs()
            chunk["is_offer"]  = (chunk["levelTo"] > chunk["levelFrom"]).astype(int)
            chunk["is_bid"]    = (chunk["levelTo"] < chunk["levelFrom"]).astype(int)
            chunk["bucket"]    = chunk["timeFrom"].dt.floor("30min")
            agg = chunk.groupby("bucket").agg(
                boalf_count       = ("delta_mw", "count"),
                boalf_offer_count = ("is_offer", "sum"),
                boalf_bid_count   = ("is_bid", "sum"),
                boalf_total_mw    = ("delta_mw", "sum"),
                boalf_max_mw      = ("delta_mw", "max"),
                boalf_so_count    = ("soFlag",   "sum"),
            )
            boalf_chunks.append(agg)
        if boalf_chunks:
            boalf = pd.concat(boalf_chunks).groupby(level=0).sum()
            # boalf_max_mw: max of maxes (sum is wrong)
            boalf["boalf_max_mw"] = pd.concat([c["boalf_max_mw"] for c in boalf_chunks]) \
                                       .groupby(level=0).max()
        del boalf_chunks; gc.collect()
    else:
        log("  WARN: neither boalf_aggregates.csv nor bmrs_boalf.csv found")

    if boalf is not None and not boalf.empty:
        df = df.join(boalf, how="left")
        for c in ["boalf_count", "boalf_offer_count", "boalf_bid_count",
                  "boalf_total_mw", "boalf_max_mw", "boalf_so_count"]:
            df[c] = df[c].fillna(0)
        report("boalf_total_mw", df, TOTAL)
    else:
        # No BOALF available → models depending on it get NaN/0 features.
        # Tree models (Model B, C) handle NaN natively.
        for c in ["boalf_count", "boalf_offer_count", "boalf_bid_count",
                  "boalf_total_mw", "boalf_max_mw", "boalf_so_count"]:
            df[c] = 0
        log("  ! BOALF features defaulted to 0 (will degrade Model B/C accuracy)")

    # ============================================================
    # Phase 7 — NESO ancillary clearing
    # ============================================================
    log("Phase 7: NESO ancillary clearing")
    anc = pd.read_csv(RAW / "neso_ancillary_clearing.csv")
    anc["deliveryStart"] = pd.to_datetime(anc["deliveryStart"], utc=True, format="mixed")
    anc_pivot = anc.pivot_table(index="deliveryStart", columns="service",
                                values="clearingPrice", aggfunc="mean")
    anc_pivot.columns = [f"anc_{c.lower()}_price" for c in anc_pivot.columns]
    # Ancillary windows are 4 hours; forward-fill to half-hourly within each block (max 7 SP fills)
    anc_30min = anc_pivot.resample("30min").ffill(limit=7)
    df = df.join(anc_30min, how="left")
    for c in ["anc_dch_price", "anc_dcl_price", "anc_dmh_price",
              "anc_dml_price", "anc_drh_price", "anc_drl_price"]:
        report(c, df, TOTAL)
    del anc, anc_pivot, anc_30min; gc.collect()

    # ============================================================
    # Phase 8 — NESO demand + wind forecasts
    # ============================================================
    log("Phase 8: NESO DA wind forecast")
    wf = pd.read_csv(RAW / "neso_da_wind_forecast.csv")
    wf["Datetime_GMT"] = pd.to_datetime(wf["Datetime_GMT"], utc=True, format="mixed")
    wf = wf.sort_values("Datetime_GMT").drop_duplicates("Datetime_GMT", keep="last")
    wf = wf[["Datetime_GMT", "Capacity", "Incentive_forecast"]].rename(columns={
        "Capacity": "wind_capacity_mw", "Incentive_forecast": "da_wind_forecast_mw",
    }).set_index("Datetime_GMT")
    df = df.join(wf, how="left")
    report("da_wind_forecast_mw", df, TOTAL)
    del wf; gc.collect()

    log("Phase 8b: NESO DA demand forecast (cardinal-point interpolation)")
    dem_f = pd.read_csv(RAW / "neso_da_demand_forecast.csv")
    dem_f["TARGETDATE"] = pd.to_datetime(dem_f["TARGETDATE"], errors="coerce")
    dem_f = dem_f.dropna(subset=["FORECASTDEMAND", "CP_ST_TIME", "TARGETDATE"])
    def cp_to_min(t):
        try:
            t = int(t); return (t // 100) * 60 + (t % 100)
        except Exception:
            return np.nan
    dem_f["minutes"] = dem_f["CP_ST_TIME"].apply(cp_to_min)
    dem_f = dem_f.dropna(subset=["minutes"])
    dem_f["timestamp"] = (dem_f["TARGETDATE"] +
                          pd.to_timedelta(dem_f["minutes"], unit="m")).dt.tz_localize("UTC")
    if "FORECAST_TIMESTAMP" in dem_f.columns:
        dem_f = dem_f.sort_values("FORECAST_TIMESTAMP").drop_duplicates("timestamp", keep="last")
    else:
        dem_f = dem_f.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    dem_cp = dem_f.set_index("timestamp")[["FORECASTDEMAND"]].rename(
        columns={"FORECASTDEMAND": "da_demand_forecast_mw"})
    dem_30min = dem_cp.resample("30min").mean().interpolate(method="time", limit=12)
    df = df.join(dem_30min, how="left")
    report("da_demand_forecast_mw", df, TOTAL)
    del dem_f, dem_cp, dem_30min; gc.collect()

    # ============================================================
    # Phase 9 — Gas: ONLY use Spectron NBP DA from DA_Prices.xlsx.
    # FRED European TTF was the old proxy and is now dropped — no model
    # script reads it. NBP is the canonical gas feature for Model A LEAR,
    # Model A LightGBM extras, and Model D.
    # ============================================================
    log("Phase 9 (SKIPPED): FRED Europe TTF gas removed — not used by any model")

    # ============================================================
    # Phase 10 — Spectron NBP gas (proprietary) → canonical 'gas_nbp_p_therm'
    # ============================================================
    log("Phase 10: Spectron NBP DA gas (DA_Prices.xlsx) → canonical 'gas_nbp_p_therm'")
    nbp = pd.read_excel(PROPRIETARY / "DA_Prices.xlsx")
    nbp_wide = nbp.pivot_table(index="DateTimeStamp", columns="PriceTypeName", values="Price")
    nbp_wide["mid"] = (nbp_wide["Bid"] + nbp_wide["Offer"]) / 2
    nbp_daily = nbp_wide[["mid"]].reset_index()
    nbp_daily.columns = ["date", "nbp_mid"]
    nbp_daily["date"] = pd.to_datetime(nbp_daily["date"]).dt.tz_localize("UTC")
    nbp_indexed = nbp_daily.set_index("date")["nbp_mid"]
    nbp_30min = nbp_indexed.reindex(df.index, method="ffill")
    df["gas_nbp_p_therm"] = nbp_30min.values        # UK NBP DA mid, pence/therm
    report("gas_nbp_p_therm", df, TOTAL)
    del nbp, nbp_wide, nbp_daily, nbp_indexed; gc.collect()

    # ============================================================
    # Phase 11 — Spectron forward curves (for LightGBM-extras feature set)
    # ============================================================
    log("Phase 11: Spectron forward curves (Jan22→May26 file)")
    spec_path = PROPRIETARY / "Spectron_Jan22_to_May26.xlsx"
    if spec_path.exists():
        spec = pd.read_excel(spec_path)
        spec["PriceDate"] = pd.to_datetime(spec["PriceDate"])
        spec["mid"] = (spec["Bid"] + spec["Offer"]) / 2
        def grab(instrument, seq_contains):
            sub = spec[(spec["InstrumentName"] == instrument) &
                       (spec["SequenceName"].astype(str).str.contains(seq_contains, na=False, case=False))]
            return sub.groupby("PriceDate")["mid"].first()
        daily = pd.DataFrame({
            "nbp_gas_da_full": grab("NBP", "Day Ahead"),
            "uk_bsld_m1_full": grab("UK Bsld", "Month"),
            "uk_peak_m1_full": grab("UK Peaks WD 3+4+5", "Month"),
            "uka_m1_full":     grab("UKA Futures (ICE Cleared) – UKA", "Emission"),
        })
        daily.index = pd.to_datetime(daily.index).tz_localize("UTC")
        spec_30min = daily.resample("30min").ffill()
        df = df.join(spec_30min, how="left")
        for c in ["nbp_gas_da_full", "uk_bsld_m1_full", "uk_peak_m1_full", "uka_m1_full"]:
            report(c, df, TOTAL)
        del spec, daily, spec_30min; gc.collect()
    else:
        log("  WARN: Spectron forward-curves file missing")

    # ============================================================
    # Phase 12 — UKA carbon (proprietary CSV)
    # ============================================================
    log("Phase 12: UKA carbon (interpolated CSV)")
    uka = pd.read_csv(PROPRIETARY / "uka_daily_interpolated.csv")
    date_col = next((c for c in uka.columns if "date" in c.lower()), uka.columns[0])
    val_col  = next((c for c in uka.columns if c != date_col), uka.columns[-1])
    uka[date_col] = pd.to_datetime(uka[date_col], utc=True, errors="coerce")
    uka = uka.dropna(subset=[date_col])
    uka_s = uka.set_index(date_col)[val_col].resample("30min").ffill()
    df["uka_eur"] = uka_s.reindex(df.index).ffill()
    report("uka_eur", df, TOTAL)
    del uka, uka_s; gc.collect()

    # ============================================================
    # Phase 13 — Brent crude (Yahoo)
    # ============================================================
    log("Phase 13: Brent crude (Yahoo)")
    if (RAW / "yahoo_brent.csv").exists():
        br = pd.read_csv(RAW / "yahoo_brent.csv")
        br["date"] = pd.to_datetime(br["date"], utc=True, errors="coerce")
        br = br.dropna(subset=["date"])
        br_s = br.set_index("date")["brent_usd"].resample("30min").ffill()
        df["brent_usd"] = br_s.reindex(df.index).ffill()
        report("brent_usd", df, TOTAL)
        del br, br_s; gc.collect()
    else:
        log("  WARN: Brent CSV missing")

    # ============================================================
    # Phase 14 — Calendar + engineered features
    # ============================================================
    log("Phase 14: Calendar + engineered features (lags + spikes + spreads)")
    df["hour"]        = df["uk_local"].dt.hour
    df["day_of_week"] = df["uk_local"].dt.dayofweek
    df["month"]       = df["uk_local"].dt.month
    df["quarter"]     = df["uk_local"].dt.quarter
    df["is_weekend"]  = (df["day_of_week"] >= 5).astype(int)
    uk_hol = holidays.UK(years=range(2021, 2028))
    df["is_holiday"]  = df["settlement_date"].map(lambda d: 1 if d in uk_hol else 0)
    df["efa_block"]   = ((df["settlement_period"] - 1) // 8) + 1

    df["wind_penetration_mw"]  = df["gen_pct_wind"] * df["actual_demand_mw"] / 100.0
    df["wind_pct_of_demand"]   = df["gen_pct_wind"]
    df["sbp_ssp_spread"]       = df["sbp"] - df["ssp"]
    df["intraday_da_spread"]   = df["mid_price"] - df["da_price"]
    df["wind_actual_proxy_mw"] = df["wind_penetration_mw"]

    df["da_price_lag_1"]   = df["da_price"].shift(1)
    df["da_price_lag_48"]  = df["da_price"].shift(48)
    df["da_price_lag_336"] = df["da_price"].shift(336)
    df["sbp_lag_1"]        = df["sbp"].shift(1)
    df["sbp_lag_48"]       = df["sbp"].shift(48)
    # ROLLING STATS — explicit .shift(1) so the window ends at t-1 (NOT t).
    # rolling(N).mean() at row t includes row t by default; for leak-free use
    # in Model B/C the rolling stats must NOT see the target value at t.
    df["da_price_roll7d_mean"] = df["da_price"].shift(1).rolling(48 * 7, min_periods=24).mean()
    df["da_price_roll7d_std"]  = df["da_price"].shift(1).rolling(48 * 7, min_periods=24).std()
    df["sbp_roll7d_mean"]      = df["sbp"].shift(1).rolling(48 * 7, min_periods=24).mean()
    df["sbp_roll7d_std"]       = df["sbp"].shift(1).rolling(48 * 7, min_periods=24).std()
    df["niv_roll7d_mean"]      = df["niv"].shift(1).rolling(48 * 7, min_periods=24).mean()
    df["demand_forecast_error"] = df["actual_demand_mw"] - df["da_demand_forecast_mw"]
    df["wind_forecast_error"]   = df["wind_actual_proxy_mw"] - df["da_wind_forecast_mw"]

    # Spike labels — calibrated on 2021–2024 to stay leak-free
    mask_train = df.index < pd.Timestamp("2025-01-01", tz="UTC")
    sbp_q95 = df.loc[mask_train, "sbp"].quantile(0.95)
    sbp_q99 = df.loc[mask_train, "sbp"].quantile(0.99)
    df["spike_label_q95"] = (df["sbp"] > sbp_q95).astype(int)
    df["spike_label_q99"] = (df["sbp"] > sbp_q99).astype(int)
    df["spike_label_200"] = (df["sbp"] > 200).astype(int)

    log(f"  Spike thresholds (train-window calibrated): q95={sbp_q95:.2f}, q99={sbp_q99:.2f}")

    # ============================================================
    # Phase 15 — Save + coverage report
    # ============================================================
    log("Phase 15: Saving master parquet")
    out_pq = PROCESSED / "master.parquet"
    df = df.drop(columns=["uk_local"])
    # settlement_date is Python datetime.date objects (object dtype) — cast to
    # pandas datetime64 so fastparquet/pyarrow can encode without an
    # "infer object conversion type" error.
    df["settlement_date"] = pd.to_datetime(df["settlement_date"])
    df.to_parquet(out_pq, compression="snappy")
    log(f"  ✓ {out_pq}  ({out_pq.stat().st_size / 1024 / 1024:.0f} MB)")

    # Coverage by day for headline columns
    log("Phase 16: Computing per-day coverage of headline cols")
    by_day = df.groupby(df["settlement_date"])[["da_price", "sbp", "actual_demand_mw",
                                                "gen_pct_gas", "gen_pct_wind",
                                                "boalf_total_mw", "gas_nbp_p_therm",
                                                "anc_dch_price", "anc_drl_price"]] \
              .apply(lambda g: pd.Series({c: g[c].notna().sum() for c in g.columns}))
    full_days = (by_day == 48).all(axis=1).sum()
    total_days = len(by_day)
    log(f"  Days with full 48-SP coverage across ALL headline cols: {full_days:,} / {total_days:,}")
    log(f"  Days with full DA-price coverage only: {(by_day['da_price'] == 48).sum():,} / {total_days:,}")
    log(f"  Days with full SBP coverage only:      {(by_day['sbp']      == 48).sum():,} / {total_days:,}")

    # Write coverage report
    coverage_file = PROCESSED / "master_coverage.txt"
    with open(coverage_file, "w", encoding="utf-8") as f:
        f.write(f"CLEAN PIPELINE — MASTER COVERAGE REPORT\n")
        f.write(f"Built at: {datetime.utcnow().isoformat()}Z\n")
        f.write(f"Window:   {START} → {END}  ({TOTAL:,} 30-min rows)\n")
        f.write(f"Output:   {out_pq}\n\n")
        f.write("PER-COLUMN COVERAGE (non-NaN row count):\n")
        f.write("\n".join(coverage_report))
        f.write("\n\nPER-DAY HEADLINE COVERAGE:\n")
        f.write(f"  Full-coverage-all days: {full_days:,} / {total_days:,}\n")
        f.write(f"  Full DA-price days:     {(by_day['da_price'] == 48).sum():,} / {total_days:,}\n")
        f.write(f"  Full SBP days:          {(by_day['sbp']      == 48).sum():,} / {total_days:,}\n")
    log(f"  ✓ Coverage report: {coverage_file}")

    log(f"\nBuild master complete in {(time.time()-t0)/60:.1f} min.")


if __name__ == "__main__":
    main()
