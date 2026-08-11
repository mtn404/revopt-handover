import { Card, Badge } from "@/components/Card";
import { DaPriceChart } from "@/components/charts/DaPriceChart";
import { WeeklyForecast } from "@/components/charts/WeeklyForecast";
import { SpikeHeatmap } from "@/components/charts/SpikeHeatmap";
import { getSnapshot } from "@/lib/data";
import forecasts7d from "@/data/forecasts_7day.json";

export default function ForecastsPage() {
  const s = getSnapshot();
  const weekly = forecasts7d.days as Array<{
    date: string; label: string; weekday?: string;
    da_mean: number; da_peak: number; da_trough: number;
    sbp_mean: number; sbp_peak: number; sbp_trough: number;
    spike_periods: number;
    ancillary_clearing: Record<string, number>;
  }>;
  const spikeGrid = forecasts7d.spike_grid_today as { period: number; time: string; prob: number }[];

  // Detect non-consecutive days (sparse-data filtering may produce gaps)
  const hasGaps = (() => {
    for (let i = 1; i < weekly.length; i++) {
      const prev = new Date(weekly[i - 1].date).getTime();
      const cur  = new Date(weekly[i].date).getTime();
      if (cur - prev > 24 * 3600 * 1000 + 60000) return true;
    }
    return false;
  })();

  // FFR omitted — legacy product, see Methodology §4.4.4
  const ancRows = [
    { key: "dch", name: "DC-H", direction: "high" as const },
    { key: "dcl", name: "DC-L", direction: "low" as const },
    { key: "dmh", name: "DM-H", direction: "high" as const },
    { key: "dml", name: "DM-L", direction: "low" as const },
    { key: "drh", name: "DR-H", direction: "high" as const },
    { key: "drl", name: "DR-L", direction: "low" as const },
  ];

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-fg tracking-tight">7-Day Forecast Horizon</h2>
        <p className="text-sm text-fg-muted mt-1">
          Walk-forward predictions from Models A (DA, ensemble), B (SBP), C (spike) and D (ancillary).
          The LP optimises across this entire window but only commits the next 24 hours.
        </p>
      </div>

      <Card title="Recent forecast performance"
            subtitle={`${weekly.length > 0 ? `${weekly[0].date} → ${weekly[weekly.length - 1].date}` : "—"} · the most recent ${weekly.length} days with full Model A coverage`}
            action={<Badge variant="brand">Last full-data window</Badge>}>
        {hasGaps && (
          <div className="mb-3 p-3 bg-bg-elevated/60 border border-bg-border rounded text-[11px] text-fg-muted leading-relaxed">
            <span className="font-medium text-fg">Note:</span> these are the most recent days with
            complete forecast coverage; calendar gaps mean BMRS hadn&apos;t fully published actuals for the
            in-between days when the pipeline ran. The next weekly cron will fill them in once the data
            settles.
          </div>
        )}
        <WeeklyForecast data={weekly} />
      </Card>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="lg:col-span-2" title="Today's day-ahead price forecast"
              subtitle="Half-hourly, Model A ensemble (LEAR + LightGBM)">
          <DaPriceChart data={s.forecast_da_today} height={260} />
        </Card>

        <Card title="Forecast accuracy" subtitle={`Walk-forward MAE · overall (since 2022) vs current year (${s.model_metrics?.current_year ?? "—"})`}>
          {(() => {
            const m = s.model_metrics;
            if (!m) return <div className="text-sm text-fg-muted">Metrics not yet computed</div>;
            const cy = m.current_year_metrics ?? {};
            const fmt = (v: number | null | undefined, suffix = "") =>
              v == null ? "—" : `${v.toFixed(2)}${suffix}`;
            const fmt4 = (v: number | null | undefined) =>
              v == null ? "—" : v.toFixed(4);
            const row = (label: string, overall: string, current: string) => (
              <div className="flex items-center justify-between pb-3 border-b border-bg-border last:border-0 last:pb-0">
                <span className="text-fg-muted">{label}</span>
                <span className="tabular text-fg flex items-center gap-2">
                  <span>{overall}</span>
                  <span className="text-[10px] text-fg-subtle">·{" "}{current} {m.current_year}</span>
                </span>
              </div>
            );
            return (
              <div className="space-y-3 text-sm">
                {row("Model A — Day-ahead",
                     `£${fmt(m.model_a_ensemble_mae_gbp_mwh)} / MWh`,
                     `£${fmt(cy.model_a_ensemble_mae)}`)}
                {row("Model B — SBP imbalance",
                     `£${fmt(m.model_b_sbp_mae_gbp_mwh)} / MWh`,
                     `£${fmt(cy.model_b_sbp_mae)}`)}
                {row("Model C — Spike AUC-ROC",
                     fmt4(m.model_c_spike_auc_roc),
                     fmt4(cy.model_c_spike_auc_roc))}
                {row("Model C — Spike F1 (@ 0.5)",
                     fmt4(m.model_c_spike_f1),
                     fmt4(cy.model_c_spike_f1))}
                {row("Model D — Ancillary",
                     `£${fmt(m.model_d_anc_mae_gbp_mw_h)} / MW·h`,
                     `£${fmt(cy.model_d_anc_mae)}`)}
              </div>
            );
          })()}
        </Card>
      </div>

      <Card title="Spike probability — today"
            subtitle="P(SBP > £200/MWh) per settlement period (Model C)">
        {(() => {
          const maxProb = Math.max(...spikeGrid.map((c) => c.prob));
          const meanProb = spikeGrid.reduce((a, c) => a + c.prob, 0) / spikeGrid.length;
          const peakSp = spikeGrid.reduce((m, c) => (c.prob > m.prob ? c : m), spikeGrid[0]);
          const quiet = maxProb < 0.10;
          return (
            <>
              {quiet ? (
                <div className="mb-3 p-3 bg-bg-elevated/60 border border-bg-border rounded text-[12px] text-fg-muted leading-relaxed">
                  <span className="font-medium text-fg">No high-confidence spikes forecast today.</span>{" "}
                  Maximum probability {(maxProb * 100).toFixed(1)}% at {peakSp.time}; mean {(meanProb * 100).toFixed(2)}%.
                  Model C correctly stays calm during quiet imbalance regimes —
                  cells appear nearly transparent because every SP is well below the £200/MWh spike threshold.
                </div>
              ) : (
                <div className="mb-3 p-3 bg-accent-amber/10 border border-accent-amber/30 rounded text-[12px] text-fg leading-relaxed">
                  <span className="font-medium">Peak spike probability {(maxProb * 100).toFixed(0)}%</span> at {peakSp.time}.
                  The LP will weight DA-position commits against potential spike capture in these periods.
                </div>
              )}
              <SpikeHeatmap data={spikeGrid} />
            </>
          );
        })()}
      </Card>

      <Card title="Ancillary clearing forecast — same window"
            subtitle="Daily-average clearing price per product (£/MW/h), from Model D">
        <div className="overflow-x-auto -mx-5 px-5">
          <table className="w-full text-sm tabular">
            <thead>
              <tr className="text-[11px] uppercase text-fg-muted tracking-wider border-b border-bg-border">
                <th className="text-left py-2 font-medium">Product</th>
                {weekly.map((d) => (
                  <th key={d.date} className="text-right py-2 font-medium whitespace-nowrap">
                    <div>{d.label}</div>
                    <div className="text-[9px] text-fg-subtle font-normal normal-case">{d.weekday ?? ""}</div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ancRows.map(({ key, name, direction }) => (
                <tr key={key} className="border-b border-bg-border/50 hover:bg-bg-elevated/50">
                  <td className="py-2.5 flex items-center gap-2">
                    <span className={`inline-block h-1.5 w-1.5 rounded-full ${
                      direction === "high" ? "bg-brand" : "bg-accent-gold"
                    }`} />
                    <span className="text-fg">{name}</span>
                  </td>
                  {weekly.map((d) => (
                    <td key={d.date} className="text-right py-2.5 text-fg">
                      £{d.ancillary_clearing[key].toFixed(1)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
