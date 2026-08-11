"use client";

import { Card, Stat, Badge } from "@/components/Card";
import { DispatchChart } from "@/components/charts/DispatchChart";
import { DaPriceChart } from "@/components/charts/DaPriceChart";
import { CompactAncillaryStrip } from "@/components/charts/CompactAncillaryStrip";
import { DailyRevenueChart } from "@/components/charts/DailyRevenueChart";
import { getSnapshot } from "@/lib/data";
import { fmtGBP, fmtPct } from "@/lib/utils";
import { useAsset, scaleMW, scaleRevenue } from "@/lib/asset-context";
import { computeDuosDayCharge, DUOS_REGIONS } from "@/lib/duos";
import { TrendingUp, Battery, Gauge, Activity } from "lucide-react";

export default function Dashboard() {
  const s = getSnapshot();
  const k = s.kpis;
  const { spec } = useAsset();

  // Scale dispatch + ancillary MW for the user's selected spec
  const scaledDispatch = s.dispatch_today.map((d) => ({
    ...d,
    pd_mw: scaleMW(d.pd_mw, spec),
    pc_mw: scaleMW(d.pc_mw, spec),
    net_mw: scaleMW(d.net_mw, spec),
    da_pos_mw: scaleMW(d.da_pos_mw, spec),
  }));

  const peakDischarge = Math.max(...scaledDispatch.map((d) => d.net_mw));
  const peakCharge    = Math.min(...scaledDispatch.map((d) => d.net_mw));
  const peakDaPrice   = Math.max(...s.forecast_da_today.map((d) => d.price));
  // Cycles today = sum of discharge MWh / energy capacity
  const totalDischargeMWh = scaledDispatch.reduce((a, d) => a + Math.max(0, d.pd_mw) * 0.5, 0);
  const cyclesToday = totalDischargeMWh / (spec.power_mw * spec.duration_h);

  const totalAncMw = s.ancillary_bids_today.reduce(
    (acc, p) => acc + p.blocks.reduce((a, b) => a + b.mw, 0),
    0
  );
  const scaledAncMw = scaleMW(totalAncMw, spec);
  const activeProducts = s.ancillary_bids_today.filter((p) =>
    p.blocks.some((b) => b.mw > 0)
  ).length;

  const todayGross = scaleRevenue(k.today_gross_gbp, spec);
  const ytdGross   = scaleRevenue(k.ytd_gross_gbp, spec);
  // Prefer trailing 12-month per-MW when the snapshot provides it (post 2026-07-09);
  // fall back to legacy YTD-annualised for older snapshots.
  const perMwSourceRaw   = k.rolling_12m_per_mw_gbp ?? k.ytd_per_mw_gbp;
  const perMwIsRolling12 = k.rolling_12m_per_mw_gbp != null;
  const perMwFullYear    = k.rolling_12m_full_year ?? false;
  const ytdPerMW         = scaleRevenue(perMwSourceRaw * 50, spec) / spec.power_mw;

  // DUoS deductions (live, reactive to region/voltage on Settings page)
  // dispatch_date is the actual day the dispatch chart represents
  // (the LP recommends this day's dispatch; may be today or tomorrow
  //  depending on the cron's iteration end).
  const dispatchDateStr = s.freshness?.dispatch_date ?? s.freshness?.last_data_through;
  const dispatchDate = dispatchDateStr
    ? new Date(dispatchDateStr + "T12:00:00Z")
    : new Date();
  const dispatchDateLabel = dispatchDate.toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });
  // Relative framing: "Today" / "Tomorrow" / "Yesterday" / date-only
  const dispatchRelative = (() => {
    if (!dispatchDateStr) return "";
    const today = new Date();
    today.setUTCHours(12, 0, 0, 0);
    const diff = Math.round((dispatchDate.getTime() - today.getTime()) / 86400000);
    if (diff === 0)  return "Today's";
    if (diff === 1)  return "Tomorrow's";
    if (diff === -1) return "Yesterday's";
    if (diff >  1)   return `In ${diff} days:`;
    return `Latest available · ${-diff} days ago:`;
  })();
  const todayDuos       = computeDuosDayCharge(scaledDispatch, dispatchDate, spec.region, spec.voltage);
  const annualDuosProxy = todayDuos * 365;          // today × 365 first-order estimate
  const todayNet        = todayGross - todayDuos;
  const annualDuosPerMW = annualDuosProxy / spec.power_mw;
  const ytdNetPerMW     = ytdPerMW - annualDuosPerMW;
  const regionShort     = DUOS_REGIONS[spec.region].name.split(" ")[0];

  // Last COMPLETED month — exclude any month flagged partial (current month
  // is usually partial; using it for headline %PF is misleading).
  const completedMonths = s.ytd_revenue_by_month.filter((m) => !m.partial);
  const lastMonth       = completedMonths[completedMonths.length - 1]
                       ?? s.ytd_revenue_by_month[s.ytd_revenue_by_month.length - 1];
  const lastMonthPct    = (lastMonth.revenue / lastMonth.pf) * 100;
  const lastMonthRev    = scaleRevenue(lastMonth.revenue, spec);
  const lastMonthPF     = scaleRevenue(lastMonth.pf, spec);
  const lastMonthLabel  = new Date(lastMonth.month + "-01").toLocaleDateString("en-GB", {
    month: "short", year: "numeric",
  });
  // Month-on-month delta in % PF (vs the completed month before)
  const prevMonth   = completedMonths[completedMonths.length - 2];
  const prevPct     = prevMonth ? (prevMonth.revenue / prevMonth.pf) * 100 : lastMonthPct;
  const deltaPct    = lastMonthPct - prevPct;
  // Partial current month info for the hint
  const partialMonth = s.ytd_revenue_by_month.find((m) => m.partial);

  return (
    <div className="space-y-6">
      {/* KPI ROW */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <Card>
          <Stat
            label="Today's net revenue"
            value={fmtGBP(todayNet)}
            hint={`${spec.power_mw} MW battery · after ${regionShort} ${spec.voltage.toUpperCase()} DUoS`}
          />
        </Card>
        <Card>
          <Stat
            label={perMwIsRolling12 ? "Rolling 12-month £/MW (net)" : "Annualised £/MW (net)"}
            value={fmtGBP(ytdNetPerMW, { compact: true })}
            hint={
              perMwIsRolling12
                ? (perMwFullYear
                    ? `Trailing 365 days · net of DUoS`
                    : `Partial window projected to 365d · net of DUoS`)
                : `Net of DUoS`
            }
          />
        </Card>
        <Card>
          <Stat
            label={`% of perfect-foresight · ${lastMonthLabel}`}
            value={fmtPct(lastMonthPct)}
            delta={`${deltaPct >= 0 ? "+" : ""}${deltaPct.toFixed(1)} pp vs prev mo`}
            deltaPositive={deltaPct >= 0}
            hint={`Last completed month · ${fmtGBP(lastMonthRev, { compact: true })} of ${fmtGBP(lastMonthPF, { compact: true })} theoretical`}
          />
        </Card>
        <Card>
          <Stat
            label="Active anc products"
            value={`${activeProducts} / 6`}
            hint={`${scaledAncMw.toFixed(0)} MW total committed today`}
          />
        </Card>
      </div>

      {/* DISPATCH + DA PRICE */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card
          className="lg:col-span-2"
          title={`${dispatchRelative} recommended dispatch — ${dispatchDateLabel}`}
          subtitle={`${spec.power_mw} MW / ${spec.power_mw * spec.duration_h} MWh · SoC carry-in: ${(s.asset.soc_carry_in_pct ?? s.asset.soc_start_pct ?? 50).toFixed(0)}% from previous day`}
          action={<Badge variant="brand">Forward solve</Badge>}
        >
          <DispatchChart data={scaledDispatch} />

          {/* Ancillary commitments — what to bid and when */}
          <div className="mt-4 pt-4 border-t border-bg-border">
            <div className="flex items-center justify-between mb-2">
              <div className="text-[11px] uppercase text-fg-muted tracking-wider font-medium">
                Ancillary commitments by 4-hour block
              </div>
              <div className="text-[10px] text-fg-subtle">
                <span style={{ color: "#2E6EE8" }} className="font-semibold">↑</span> HIGH (discharge family) ·{" "}
                <span style={{ color: "#F0AB36" }} className="font-semibold">↓</span> LOW (charge family)
              </div>
            </div>
            <CompactAncillaryStrip
              products={s.ancillary_bids_today.map((p) => ({
                ...p,
                blocks: p.blocks.map((b) => ({ ...b, mw: Math.round(scaleMW(b.mw, spec)) })),
              }))}
            />
          </div>

          <div className="grid grid-cols-3 gap-4 mt-4 pt-4 border-t border-bg-border">
            <div>
              <div className="text-[11px] uppercase text-fg-muted tracking-wider">Peak discharge</div>
              <div className="text-base font-semibold text-brand tabular mt-0.5">
                +{peakDischarge.toFixed(0)} MW
              </div>
            </div>
            <div>
              <div className="text-[11px] uppercase text-fg-muted tracking-wider">Peak charge</div>
              <div className="text-base font-semibold text-accent-amber tabular mt-0.5">
                {peakCharge.toFixed(0)} MW
              </div>
            </div>
            <div>
              <div className="text-[11px] uppercase text-fg-muted tracking-wider">Cycles today</div>
              <div className="text-base font-semibold text-fg tabular mt-0.5">{cyclesToday.toFixed(1)} / 2.0</div>
            </div>
          </div>
        </Card>

        <Card
          title={`${dispatchRelative} day-ahead forecast`}
          subtitle={`Half-hourly profile · ${dispatchDateLabel}`}
          action={<Badge>Model A — Ensemble</Badge>}
        >
          <DaPriceChart data={s.forecast_da_today} height={224} />
          <div className="grid grid-cols-2 gap-4 mt-4 pt-4 border-t border-bg-border">
            <div>
              <div className="text-[11px] uppercase text-fg-muted tracking-wider">Peak DA</div>
              <div className="text-base font-semibold text-fg tabular mt-0.5">
                £{peakDaPrice.toFixed(0)}/MWh
              </div>
            </div>
            <div>
              <div className="text-[11px] uppercase text-fg-muted tracking-wider">Forecast MAE</div>
              <div className="text-base font-semibold text-fg tabular mt-0.5">
                £{(s.model_metrics?.model_a_ensemble_mae_gbp_mwh ?? 0).toFixed(2)}
              </div>
            </div>
          </div>
        </Card>
      </div>

      {/* OPERATIONAL READOUTS */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card title={`${dispatchRelative} ancillary commitments`}
              subtitle={dispatchDateLabel}>
          <div className="space-y-2">
            {s.ancillary_bids_today.map((p) => {
              const total = scaleMW(p.blocks.reduce((a, b) => a + b.mw, 0), spec);
              const active = p.blocks.filter((b) => b.mw > 0).length;
              return (
                <div key={p.product} className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-2">
                    <span
                      className={`inline-block h-1.5 w-1.5 rounded-full ${
                        p.direction === "high" ? "bg-brand" : "bg-accent-gold"
                      }`}
                    />
                    <span className="text-fg-muted text-xs">{p.product}</span>
                    <span className="text-fg-subtle text-xs">— {p.name}</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="text-[11px] text-fg-subtle">{active}/6 blocks</span>
                    <span className="text-fg tabular w-12 text-right">{total.toFixed(0)} MW</span>
                  </div>
                </div>
              );
            })}
          </div>
        </Card>

        <Card title={`${dispatchRelative} SoC plan`} subtitle={`Trajectory · ${dispatchDateLabel}`}>
          <div className="space-y-4">
            {(() => {
              const carryIn = s.asset.soc_carry_in_pct ?? s.asset.soc_start_pct ?? 50;
              return (
                <div>
                  <div className="flex items-center justify-between text-xs text-fg-muted mb-1.5">
                    <span>Carried forward from yesterday</span>
                    <span className="tabular text-fg">{carryIn.toFixed(0)}%</span>
                  </div>
                  <div className="h-2 bg-bg-elevated rounded">
                    <div
                      className="h-full bg-accent-teal rounded"
                      style={{ width: `${carryIn}%` }}
                    />
                  </div>
                </div>
              );
            })()}
            <div>
              <div className="flex items-center justify-between text-xs text-fg-muted mb-1.5">
                <span>End-of-day projected</span>
                <span className="tabular text-fg">
                  {s.dispatch_today[s.dispatch_today.length - 1].soc_pct.toFixed(0)}%
                </span>
              </div>
              <div className="h-2 bg-bg-elevated rounded">
                <div
                  className="h-full bg-brand rounded"
                  style={{
                    width: `${s.dispatch_today[s.dispatch_today.length - 1].soc_pct}%`,
                  }}
                />
              </div>
            </div>
            <div className="pt-3 border-t border-bg-border space-y-1.5">
              <div className="flex items-center gap-2 text-xs text-fg-muted">
                <Battery className="h-3.5 w-3.5" />
                <span>SoC bounds: 10% — 95%</span>
              </div>
              <div className="flex items-center gap-2 text-xs text-fg-muted">
                <Gauge className="h-3.5 w-3.5" />
                <span>Cycle limit: 2.0/day · used {cyclesToday.toFixed(1)} today</span>
              </div>
              <div className="flex items-center gap-2 text-xs text-fg-muted">
                <Activity className="h-3.5 w-3.5" />
                <span>RTE: {(spec.rte * 100).toFixed(0)}%</span>
              </div>
            </div>
          </div>
        </Card>

        <Card title="Daily revenue · last 14 days"
              subtitle="Realised vs perfect-foresight · weekends shown dimmed">
          {(() => {
            const last14 = s.revenue_daily_last_30d.slice(-14).map((d) => ({
              ...d,
              realised: scaleRevenue(d.realised, spec),
              pf:       scaleRevenue(d.pf, spec),
            }));
            const sum14   = last14.reduce((a, d) => a + d.realised, 0);
            const sumPF14 = last14.reduce((a, d) => a + d.pf, 0);
            const pct14   = (sum14 / sumPF14) * 100;
            return (
              <>
                <DailyRevenueChart data={last14} height={200} hideLegend />
                <div className="grid grid-cols-3 gap-3 mt-3 pt-3 border-t border-bg-border">
                  <div>
                    <div className="text-[10px] uppercase text-fg-muted tracking-wider">14d total</div>
                    <div className="text-sm font-semibold text-fg tabular mt-0.5">
                      {fmtGBP(sum14, { compact: true })}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase text-fg-muted tracking-wider">vs PF</div>
                    <div className="text-sm font-semibold text-brand tabular mt-0.5">
                      {fmtPct(pct14)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase text-fg-muted tracking-wider">Avg/day</div>
                    <div className="text-sm font-semibold text-fg tabular mt-0.5">
                      {fmtGBP(sum14 / 14, { compact: true })}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2 text-[11px] text-fg-muted mt-3 pt-2 border-t border-bg-border">
                  <TrendingUp className="h-3.5 w-3.5 text-brand" />
                  <span>Case-study window aggregate: {fmtPct(k.ytd_pct_pf)} of PF</span>
                </div>
              </>
            );
          })()}
        </Card>
      </div>
    </div>
  );
}
