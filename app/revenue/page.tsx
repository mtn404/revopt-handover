"use client";

import { Card, Badge } from "@/components/Card";
import { DailyRevenueChart } from "@/components/charts/DailyRevenueChart";
import { getSnapshot } from "@/lib/data";
import { fmtGBP, fmtPct } from "@/lib/utils";
import { useAsset, scaleRevenue, scaleMW } from "@/lib/asset-context";
import { computeDuosDayCharge, DUOS_REGIONS, DUOS_VOLTAGE_LABELS } from "@/lib/duos";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from "recharts";

export default function RevenuePage() {
  const s = getSnapshot();
  const { spec } = useAsset();

  // ---- DUoS impact for the latest-dispatch day, scaled to the user's spec ----
  const dispatchDate = s.freshness?.last_data_through
    ? new Date(s.freshness.last_data_through + "T12:00:00Z")
    : new Date();
  const scaledForDuos = s.dispatch_today.map((d) => ({
    period: d.period,
    pc_mw:  scaleMW(d.pc_mw, spec),
    pd_mw:  scaleMW(d.pd_mw, spec),
  }));
  const todayDuos = computeDuosDayCharge(scaledForDuos, dispatchDate, spec.region, spec.voltage);
  // Annualised DUoS proxy (×365); reasonable first-order estimate
  const annualDuos = todayDuos * 365;

  // ---- DAILY (last 30 days) ----
  const daily = s.revenue_daily_last_30d.map((d) => ({
    ...d,
    realised: Math.round(scaleRevenue(d.realised, spec)),
    pf:       Math.round(scaleRevenue(d.pf, spec)),
  }));
  const dailySum   = daily.reduce((a, d) => a + d.realised, 0);
  const dailySumPF = daily.reduce((a, d) => a + d.pf, 0);
  const dailyPct   = (dailySum / dailySumPF) * 100;
  const avgPerDay  = dailySum / daily.length;
  const peakDay    = daily.reduce((max, d) => (d.realised > max.realised ? d : max), daily[0]);
  const troughDay  = daily.reduce((min, d) => (d.realised < min.realised ? d : min), daily[0]);

  // ---- MONTHLY (case-study window) ----
  const months = s.ytd_revenue_by_month.map((m) => ({
    ...m,
    revenue: Math.round(scaleRevenue(m.revenue, spec)),
    pf:      Math.round(scaleRevenue(m.pf, spec)),
  }));
  const cumActual = months.reduce((a, m) => a + m.revenue, 0);
  const cumPF     = months.reduce((a, m) => a + m.pf, 0);
  const cumPct    = (cumActual / cumPF) * 100;

  // Per-MW: prefer trailing 12-month; fall back to legacy YTD-annualised
  const perMwSourceRaw   = s.kpis.rolling_12m_per_mw_gbp ?? s.kpis.ytd_per_mw_gbp;
  const perMwIsRolling12 = s.kpis.rolling_12m_per_mw_gbp != null;
  const perMwFullYear    = s.kpis.rolling_12m_full_year ?? false;
  const ytdPerMW        = scaleRevenue(perMwSourceRaw * 50, spec) / spec.power_mw;
  const annualDuosPerMW = annualDuos / spec.power_mw;
  const ytdNetPerMW     = ytdPerMW - annualDuosPerMW;
  const vsMedian        = Math.round(ytdPerMW / 50_000 * 100);

  const monthChart = months.map((m) => ({
    month: m.month.slice(5),
    Realised: m.revenue,
    Oracle:   m.pf,
  }));

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-fg tracking-tight">Revenue Realisation</h2>
        <p className="text-sm text-fg-muted mt-1">
          Daily-granularity revenue tracking — realised LP-driven gross compared to the
          architecture-matched PF v6 oracle, day-by-day. All figures for the selected{" "}
          <span className="font-medium text-fg">{spec.power_mw} MW · {spec.power_mw * spec.duration_h} MWh ({spec.duration_h}h)</span>{" "}
          battery.
        </p>
      </div>

      {/* Network charges banner — all numbers expressed per-MW-per-year so
          they are directly comparable. Prefers trailing 12-month for the LP
          gross figure (captures a full seasonal cycle); falls back to legacy
          YTD-annualised if the snapshot predates the rolling-window field. */}
      {(() => {
        const perMwAnnualised   = scaleRevenue(perMwSourceRaw * 50, spec) / spec.power_mw;
        const annualDuosPerMw   = annualDuos / spec.power_mw;
        const netPerMwAnnualised = perMwAnnualised - annualDuosPerMw;
        const duosPct           = (annualDuosPerMw / perMwAnnualised) * 100;
        const grossHintLabel    = perMwIsRolling12
          ? (perMwFullYear ? "Rolling 12 months" : "Partial · projected to 365d")
          : "YTD annualised";
        const subtitleTail      = perMwIsRolling12 ? "trailing 12-month per MW" : "annualised per MW";
        return (
          <Card
            title="Network charge impact (DUoS)"
            subtitle={`${DUOS_REGIONS[spec.region].name} · ${DUOS_VOLTAGE_LABELS[spec.voltage]} · change on Settings page · all values ${subtitleTail}`}
            action={<Badge variant="gold">Live · proxy</Badge>}
          >
            <div className="grid grid-cols-1 md:grid-cols-4 gap-3 text-sm">
              <div>
                <div className="text-[10px] uppercase text-fg-muted tracking-wider">Gross per MW · yr</div>
                <div className="text-base font-semibold text-fg tabular mt-0.5">
                  {fmtGBP(perMwAnnualised, { compact: true })}
                </div>
                <div className="text-[10px] text-fg-muted mt-0.5">{grossHintLabel}</div>
              </div>
              <div>
                <div className="text-[10px] uppercase text-fg-muted tracking-wider">DUoS deduction</div>
                <div className="text-base font-semibold text-accent-amber tabular mt-0.5">
                  −{fmtGBP(annualDuosPerMw, { compact: true })}
                </div>
                <div className="text-[10px] text-fg-muted mt-0.5">today × 365 proxy</div>
              </div>
              <div>
                <div className="text-[10px] uppercase text-fg-muted tracking-wider">Net per MW · yr</div>
                <div className="text-base font-semibold text-brand tabular mt-0.5">
                  {fmtGBP(netPerMwAnnualised, { compact: true })}
                </div>
                <div className="text-[10px] text-fg-muted mt-0.5">after DUoS</div>
              </div>
              <div>
                <div className="text-[10px] uppercase text-fg-muted tracking-wider">DUoS share</div>
                <div className="text-base font-semibold text-fg tabular mt-0.5">
                  {duosPct.toFixed(1)}%
                </div>
                <div className="text-[10px] text-fg-muted mt-0.5">of gross</div>
              </div>
            </div>
            <div className="text-[11px] text-fg-muted mt-3 pt-3 border-t border-bg-border leading-relaxed">
              All four figures are per MW per year so they are directly comparable.
              <span className="font-medium text-fg-muted"> Gross</span> = {perMwIsRolling12
                ? "LP revenue over the trailing 365 days ÷ MW"
                : "LP revenue YTD ÷ days ÷ MW × 365"}.
              <span className="font-medium text-fg-muted"> DUoS deduction</span> is computed
              from today&apos;s dispatch × 365 (rough proxy; full per-day calculation across the
              backtest is planned for the Python pipeline). Capacity charges (£/kVA), TNUoS, and
              BSUoS are NOT included.
            </div>
          </Card>
        );
      })()}

      {/* KPI tiles
          All revenue figures on this page are realised LP revenue from the dispatch
          parquet (pre-DUoS). The banner above shows the net-of-DUoS breakdown for
          context. We don't repeat the gross/net label on every tile to avoid noise. */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <Card>
          <div className="text-[11px] uppercase text-fg-muted tracking-wider">30-day total</div>
          <div className="text-2xl font-semibold text-fg tabular mt-1">{fmtGBP(dailySum, { compact: true })}</div>
          <div className="text-[11px] text-fg-muted mt-1">vs PF {fmtGBP(dailySumPF, { compact: true })} ({fmtPct(dailyPct)})</div>
        </Card>
        <Card>
          <div className="text-[11px] uppercase text-fg-muted tracking-wider">Average per day</div>
          <div className="text-2xl font-semibold text-brand tabular mt-1">{fmtGBP(avgPerDay, { compact: true })}</div>
          <div className="text-[11px] text-fg-muted mt-1">Across {daily.length} days</div>
        </Card>
        <Card>
          <div className="text-[11px] uppercase text-fg-muted tracking-wider">Best day</div>
          <div className="text-2xl font-semibold text-accent-green tabular mt-1">{fmtGBP(peakDay.realised, { compact: true })}</div>
          <div className="text-[11px] text-fg-muted mt-1">{peakDay.date} ({peakDay.weekday})</div>
        </Card>
        <Card>
          <div className="text-[11px] uppercase text-fg-muted tracking-wider">Lowest day</div>
          <div className="text-2xl font-semibold text-accent-amber tabular mt-1">{fmtGBP(troughDay.realised, { compact: true })}</div>
          <div className="text-[11px] text-fg-muted mt-1">{troughDay.date} ({troughDay.weekday})</div>
        </Card>
      </div>

      <Card
        title="Daily realised vs perfect-foresight"
        subtitle={`Last 30 days · ${daily[0].date} → ${daily[daily.length-1].date}`}
      >
        <DailyRevenueChart data={daily} height={300} />
        <p className="text-[11px] text-fg-muted mt-3 pt-3 border-t border-bg-border leading-relaxed">
          Each pair of bars = one day. Solid blue = realised LP-driven gross revenue;
          translucent gold = PF v6 oracle (the theoretical maximum for that same day under
          perfect-information dispatch). Weekend bars are dimmed for at-a-glance weekly pattern
          recognition. The gap between blue and gold is the forecasting error cost — bigger gaps
          mean the day&apos;s prices were harder to predict.
        </p>
      </Card>

      <Card
        title="Daily detail"
        subtitle="Realised vs PF, capture rate, and the day&apos;s mean day-ahead price"
      >
        <div className="overflow-x-auto -mx-5">
          <table className="w-full text-sm tabular">
            <thead className="sticky top-0 bg-bg-surface">
              <tr className="text-[11px] uppercase text-fg-muted tracking-wider border-b border-bg-border">
                <th className="text-left py-2 px-5 font-medium">Date</th>
                <th className="text-left py-2 font-medium">Day</th>
                <th className="text-right py-2 font-medium">DA mean £/MWh</th>
                <th className="text-right py-2 font-medium">Realised £</th>
                <th className="text-right py-2 font-medium">PF £</th>
                <th className="text-right py-2 font-medium">Gap £</th>
                <th className="text-right py-2 pr-5 font-medium">% PF</th>
              </tr>
            </thead>
            <tbody>
              {daily.slice().reverse().map((d) => {
                const pct = (d.realised / d.pf) * 100;
                const gap = d.pf - d.realised;
                const isWeekend = d.weekday === "Sat" || d.weekday === "Sun";
                return (
                  <tr
                    key={d.date}
                    className={`border-b border-bg-border/40 hover:bg-bg-elevated/60 ${isWeekend ? "text-fg-subtle" : ""}`}
                  >
                    <td className="py-2.5 px-5 text-fg">{d.date}</td>
                    <td className="py-2.5 text-fg-muted">{d.weekday}</td>
                    <td className="text-right py-2.5 text-fg-muted">£{d.da.toFixed(1)}</td>
                    <td className="text-right py-2.5 text-brand font-medium">{fmtGBP(d.realised, { compact: true })}</td>
                    <td className="text-right py-2.5 text-accent-gold">{fmtGBP(d.pf, { compact: true })}</td>
                    <td className="text-right py-2.5 text-fg-muted">{fmtGBP(gap, { compact: true })}</td>
                    <td className={`text-right py-2.5 pr-5 font-medium ${pct >= 85 ? "text-brand" : "text-fg"}`}>
                      {fmtPct(pct)}
                    </td>
                  </tr>
                );
              })}
              <tr className="bg-bg-elevated font-semibold">
                <td className="py-3 px-5 text-fg">30-day total</td>
                <td className="py-3 text-fg-muted">—</td>
                <td className="text-right py-3 text-fg-muted">—</td>
                <td className="text-right py-3 text-brand">{fmtGBP(dailySum, { compact: true })}</td>
                <td className="text-right py-3 text-accent-gold">{fmtGBP(dailySumPF, { compact: true })}</td>
                <td className="text-right py-3 text-fg-muted">{fmtGBP(dailySumPF - dailySum, { compact: true })}</td>
                <td className="text-right py-3 pr-5 text-brand">{fmtPct(dailyPct)}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </Card>

      {/* MONTHLY — kept as secondary aggregate view */}
      <Card
        title="Monthly aggregate · rolling 6 months"
        subtitle={(() => {
          if (!months.length) return "—";
          const fmt = (ym: string) => new Date(ym + "-01").toLocaleDateString("en-GB", { month: "short", year: "numeric" });
          return `${fmt(months[0].month)} – ${fmt(months[months.length - 1].month)} · 6-month totals for trend context`;
        })()}
      >
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={monthChart} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="month" tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} tickFormatter={(v) => `£${(v / 1000).toFixed(0)}k`} />
            <Tooltip formatter={(v: number) => `£${v.toLocaleString()}`} />
            <Legend wrapperStyle={{ fontSize: 11, paddingTop: 8 }} />
            <Bar dataKey="Realised" fill="#2E6EE8" />
            <Bar dataKey="Oracle"   fill="#E8A33D" />
          </BarChart>
        </ResponsiveContainer>
        <div className="grid grid-cols-3 gap-3 mt-3 pt-3 border-t border-bg-border">
          <div>
            <div className="text-[10px] uppercase text-fg-muted tracking-wider">6-month realised</div>
            <div className="text-base font-semibold text-fg tabular mt-0.5">{fmtGBP(cumActual, { compact: true })}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-fg-muted tracking-wider">PF oracle</div>
            <div className="text-base font-semibold text-accent-gold tabular mt-0.5">{fmtGBP(cumPF, { compact: true })}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-fg-muted tracking-wider">Window % PF</div>
            <div className="text-base font-semibold text-brand tabular mt-0.5">{fmtPct(cumPct)}</div>
          </div>
        </div>
      </Card>

      <Card title="Industry benchmarking"
            subtitle="£/MW/yr annualised · all values net of distribution network charges">
        {(() => {
          const b = s.benchmarks;
          if (!b) return <div className="text-sm text-fg-muted">Benchmarks not yet computed</div>;
          const lpNetPerMw = ytdNetPerMW;
          const max = Math.max(
            lpNetPerMw,
            b.blackhillock_per_mw_gbp_yr ?? 0,
            b.modo_top_decile_per_mw_gbp_yr ?? 0,
            b.modo_median_per_mw_gbp_yr ?? 0,
          );
          const w = (v: number) => `${Math.max(2, (v / max) * 100)}%`;
          const fmt = (v: number) => `£${(v / 1000).toFixed(0)}k/MW/yr`;
          const lines = [
            { label: "This LP",                                v: lpNetPerMw,                       color: "bg-brand",       tone: "text-brand" },
            { label: "Blackhillock (top commercial operator)", v: b.blackhillock_per_mw_gbp_yr,     color: "bg-accent-gold", tone: "text-accent-gold" },
            { label: "Industry top-decile",                    v: b.modo_top_decile_per_mw_gbp_yr,  color: "bg-fg-muted",    tone: "text-fg" },
            { label: "Industry median",                        v: b.modo_median_per_mw_gbp_yr,     color: "bg-fg-muted/50", tone: "text-fg-muted" },
          ];
          return (
            <div className="space-y-4">
              {lines.map((line) => (
                <div key={line.label}>
                  <div className="flex items-center justify-between text-xs mb-2">
                    <span className="text-fg-muted">{line.label}</span>
                    <span className={`tabular font-semibold ${line.tone}`}>{fmt(line.v ?? 0)}</span>
                  </div>
                  <div className="h-2.5 bg-bg-elevated rounded overflow-hidden">
                    <div className={`h-full ${line.color} rounded`} style={{ width: w(line.v ?? 0) }} />
                  </div>
                </div>
              ))}
              <div className="pt-3 border-t border-bg-border text-[11px] text-fg-muted leading-relaxed">
                Sources: <span className="font-medium">Modo Energy BESS Index 2024 H2</span> for industry
                top-decile and median; Modo coverage of Blackhillock for the top-operator reference.
                Industry figures and this LP are all shown net of distribution network charges.
              </div>
            </div>
          );
        })()}
      </Card>
    </div>
  );
}
