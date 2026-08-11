import snapshot from "@/data/snapshot.json";

export type ForecastPoint = { period: number; time: string; price: number };
export type DispatchPoint = {
  period: number; time: string;
  pd_mw: number; pc_mw: number; net_mw: number; da_pos_mw: number; soc_pct: number;
};
export type AncillaryBlock = {
  block: number; window: string; mw: number; price: number;
};
export type AncillaryProduct = {
  product: string; name: string; direction: "high" | "low"; blocks: AncillaryBlock[];
};

export type Freshness = {
  last_refreshed_utc: string;
  last_data_through: string;     // YYYY-MM-DD — latest LP-solved day (used for KPIs)
  dispatch_date?: string;        // YYYY-MM-DD — day shown in the dispatch chart
  last_retrained: string;        // YYYY-MM-DD
  pipeline_mode: "backtest" | "live";
};

export type ModelMetrics = {
  model_a_ensemble_mae_gbp_mwh: number | null;
  model_a_lear_mae_gbp_mwh: number | null;
  model_b_sbp_mae_gbp_mwh: number | null;
  model_c_spike_f1: number | null;
  model_c_spike_auc_roc: number | null;
  model_c_spike_auc_pr: number | null;
  model_d_anc_mae_gbp_mw_h: number | null;
  current_year: number;
  current_year_metrics: {
    model_a_ensemble_mae: number | null;
    model_b_sbp_mae: number | null;
    model_c_spike_f1: number | null;
    model_c_spike_auc_roc: number | null;
    model_d_anc_mae: number | null;
  };
};

export type Benchmarks = {
  lp_per_mw_gbp_yr: number;
  naive_da_per_mw_gbp_yr: number | null;
  modo_top_decile_per_mw_gbp_yr: number;
  modo_median_per_mw_gbp_yr: number;
  modo_source: string;
  blackhillock_per_mw_gbp_yr: number;
  blackhillock_source: string;
};

export type Snapshot = {
  generated_at: string;
  freshness?: Freshness;         // optional for backwards-compat with older snapshots
  asset: {
    name: string; site: string;
    p_max_mw: number; e_max_mwh: number; rte: number;
    /** Actual carry-in SoC (end of previous day) read from dispatch parquet.
     *  Optional for back-compat with older snapshots that used soc_start_pct. */
    soc_carry_in_pct?: number;
    soc_start_pct?: number;  // legacy field, falls back to 50 if neither present
  };
  kpis: {
    today_gross_gbp: number; today_vs_pf_pct: number;
    week_pct_pf?: number;        // 7d trailing — smooths out single-day forecast luck
    week_gross_gbp: number; ytd_gross_gbp: number;
    ytd_per_mw_gbp: number; ytd_pct_pf: number;
    // Rolling 12-month (trailing 365 days). More honest per-year figure than
    // YTD-annualised because it captures a full seasonal cycle. Present on
    // snapshots ≥ 2026-07-09; older snapshots omit these fields.
    rolling_12m_gross_gbp?:  number;
    rolling_12m_per_mw_gbp?: number;
    rolling_12m_pct_pf?:     number;
    rolling_12m_full_year?:  boolean;
    rolling_12m_days?:       number;
    vs_industry_median_pct: number;
  };
  model_metrics?: ModelMetrics;  // walk-forward eval metrics, populated by export
  benchmarks?: Benchmarks;       // industry / naive comparators
  forecast_da_today: ForecastPoint[];
  dispatch_today: DispatchPoint[];
  ancillary_bids_today: AncillaryProduct[];
  ytd_revenue_by_month: {
    month: string; revenue: number; pf: number;
    partial?: boolean; days_so_far?: number;
  }[];
  revenue_daily_last_30d: {
    date: string;
    weekday: string;
    realised: number;
    pf: number;
    da: number;
  }[];
};

// Products hidden from the MVP UI. FFR is a legacy NESO frequency-response
// product largely phased out post-2020 by Dynamic Containment / Moderation /
// Regulation. It remains in the dissertation LP for completeness but adds
// no operational value in production, so we hide it from the dashboard.
const HIDDEN_PRODUCTS = new Set(["FFR"]);

export function getSnapshot(): Snapshot {
  const raw = snapshot as Snapshot;
  return {
    ...raw,
    ancillary_bids_today: raw.ancillary_bids_today.filter(
      (p) => !HIDDEN_PRODUCTS.has(p.product)
    ),
  };
}
