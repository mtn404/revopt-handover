"use client";

import { Card, Badge } from "@/components/Card";
import { DispatchChart } from "@/components/charts/DispatchChart";
import { AncillaryTimeline } from "@/components/charts/AncillaryTimeline";
import { getSnapshot } from "@/lib/data";
import { fmtMW } from "@/lib/utils";
import { useAsset, scaleMW } from "@/lib/asset-context";

export default function DispatchPage() {
  const s = getSnapshot();
  const { spec } = useAsset();

  // Scale all per-period MW values for the selected asset
  const rows = s.dispatch_today.map((d) => ({
    ...d,
    pd_mw: scaleMW(d.pd_mw, spec),
    pc_mw: scaleMW(d.pc_mw, spec),
    net_mw: scaleMW(d.net_mw, spec),
    da_pos_mw: scaleMW(d.da_pos_mw, spec),
  }));

  const scaledProducts = s.ancillary_bids_today.map((p) => ({
    ...p,
    blocks: p.blocks.map((b) => ({ ...b, mw: Math.round(scaleMW(b.mw, spec)) })),
  }));

  // Stats
  const totalDischargeMwh = rows.reduce((a, r) => a + r.pd_mw * 0.5, 0);
  const energyCapacityMWh = spec.power_mw * spec.duration_h;
  const cycles = totalDischargeMwh / energyCapacityMWh;
  const peakDis = Math.max(...rows.map((r) => r.net_mw));
  const peakChg = Math.min(...rows.map((r) => r.net_mw));
  const endSoc = rows[rows.length - 1].soc_pct;

  const periodToBlock = (p: number) => Math.floor((p - 1) / 8) + 1;
  // Map block index → its EFA-day window (HH:MM-HH:MM). Block 1 = 23:00-03:00.
  const blockWindow = (b: number) => {
    const start = (23 + (b - 1) * 4) % 24;
    const end   = (start + 4) % 24;
    const fmt = (h: number) => `${String(h).padStart(2, "0")}:00`;
    return `${fmt(start)}-${fmt(end)}`;
  };

  const ancByBlock = new Map<number, { high: string[]; low: string[] }>();
  for (let b = 1; b <= 6; b++) {
    const high: string[] = [];
    const low: string[]  = [];
    for (const p of scaledProducts) {
      const blk = p.blocks.find((bl) => bl.block === b);
      if (blk && blk.mw > 0) {
        const label = `${p.product} ${blk.mw}MW`;
        (p.direction === "high" ? high : low).push(label);
      }
    }
    ancByBlock.set(b, { high, low });
  }

  // CSV export — matches the visible detail table columns
  const handleExportCsv = () => {
    const dateForName = s.freshness?.dispatch_date ?? s.freshness?.last_data_through ?? "dispatch";
    const header = [
      "settlement_period", "time", "efa_block",
      "discharge_mw", "charge_mw", "net_mw",
      "da_position_mw", "soc_pct",
      "ancillary_high", "ancillary_low",
    ].join(",");
    const lines = rows.map((r) => {
      const block = periodToBlock(r.period);
      const anc = ancByBlock.get(block) ?? { high: [], low: [] };
      const high = anc.high.join("; ");
      const low  = anc.low.join("; ");
      return [
        r.period, r.time, `B${block}`,
        r.pd_mw.toFixed(2), r.pc_mw.toFixed(2), r.net_mw.toFixed(2),
        r.da_pos_mw.toFixed(2), r.soc_pct.toFixed(1),
        `"${high}"`, `"${low}"`,
      ].join(",");
    });
    const csv = [header, ...lines].join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `dispatch_${dateForName}_${spec.power_mw}MW.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const dispatchDateStr = s.freshness?.dispatch_date ?? s.freshness?.last_data_through ?? "";
  const dispatchDateObj = dispatchDateStr ? new Date(dispatchDateStr + "T12:00:00Z") : new Date();
  const dispatchDateLabel = dispatchDateStr
    ? dispatchDateObj.toLocaleDateString("en-GB", {
        weekday: "long", day: "numeric", month: "long", year: "numeric",
      })
    : "";
  const dispatchRelative = (() => {
    if (!dispatchDateStr) return "Dispatch";
    const today = new Date(); today.setUTCHours(12, 0, 0, 0);
    const diff = Math.round((dispatchDateObj.getTime() - today.getTime()) / 86400000);
    if (diff === 0)  return "Today's dispatch";
    if (diff === 1)  return "Tomorrow's dispatch";
    if (diff === -1) return "Yesterday's dispatch";
    return diff > 1 ? `Dispatch (in ${diff} days)` : `Dispatch (${-diff} days ago)`;
  })();

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-fg tracking-tight">
          {dispatchRelative} &mdash; {dispatchDateLabel}
        </h2>
        <p className="text-sm text-fg-muted mt-1">
          Half-hourly recommended dispatch from the LP v6 ensemble, with ancillary product
          commitments overlaid. Optimised on the NESO EFA-day boundary (23:00 → 23:00 UTC).
          The LP re-solves with fresh forecasts and rolled-forward SoC. Asset:{" "}
          <span className="font-semibold text-fg">
            {spec.power_mw} MW · {energyCapacityMWh} MWh ({spec.duration_h}h)
          </span>.
        </p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        <Card>
          <div className="text-[11px] uppercase text-fg-muted tracking-wider">Peak discharge</div>
          <div className="text-2xl font-semibold text-brand tabular mt-1">{fmtMW(peakDis, 0)}</div>
        </Card>
        <Card>
          <div className="text-[11px] uppercase text-fg-muted tracking-wider">Peak charge</div>
          <div className="text-2xl font-semibold text-accent-amber tabular mt-1">{fmtMW(peakChg, 0)}</div>
        </Card>
        <Card>
          <div className="text-[11px] uppercase text-fg-muted tracking-wider">Energy discharged</div>
          <div className="text-2xl font-semibold text-fg tabular mt-1">{totalDischargeMwh.toFixed(0)} MWh</div>
        </Card>
        <Card>
          <div className="text-[11px] uppercase text-fg-muted tracking-wider">Cycles</div>
          <div className="text-2xl font-semibold text-fg tabular mt-1">{cycles.toFixed(2)} / 2.0</div>
        </Card>
        <Card>
          <div className="text-[11px] uppercase text-fg-muted tracking-wider">End-of-day SoC</div>
          <div className="text-2xl font-semibold text-accent-teal tabular mt-1">{endSoc.toFixed(0)}%</div>
        </Card>
      </div>

      <Card
        title="Net dispatch and state-of-charge"
        subtitle="EFA day (23:00 → 23:00 UTC). Positive (sea blue) = discharge / export. Negative (amber) = charge / import. SoC overlaid on right axis."
        action={<Badge variant="brand">Commit by 11:00 UTC</Badge>}
      >
        <DispatchChart data={rows} />
      </Card>

      <Card
        title="Ancillary product commitments — multi-product, per 4-hour EFA block"
        subtitle="The LP commits MW across multiple ancillary products simultaneously in each block; HIGH and LOW directions share separate capacity budgets."
        action={<Badge variant="brand">6 products · 6 blocks</Badge>}
      >
        <AncillaryTimeline products={scaledProducts} pMaxMw={spec.power_mw} />
      </Card>

      <Card
        title="Half-hourly dispatch detail with ancillary commitments"
        subtitle="48 settlement periods · physical dispatch + DA market commit + active ancillary products in the same EFA block"
        action={<button onClick={handleExportCsv} className="text-[11px] text-brand hover:text-brand-dark hover:underline transition-colors">Export CSV</button>}
      >
        <div className="overflow-x-auto -mx-5">
          <table className="w-full text-sm tabular">
            <thead className="sticky top-0 bg-bg-surface">
              <tr className="text-[11px] uppercase text-fg-muted tracking-wider border-b border-bg-border">
                <th className="text-left py-2 px-5 font-medium">SP</th>
                <th className="text-left py-2 font-medium">Time</th>
                <th className="text-left py-2 font-medium">Block</th>
                <th className="text-right py-2 font-medium">Disch MW</th>
                <th className="text-right py-2 font-medium">Chg MW</th>
                <th className="text-right py-2 font-medium">Net MW</th>
                <th className="text-right py-2 font-medium">DA pos</th>
                <th className="text-right py-2 font-medium">SoC %</th>
                <th className="text-left py-2 pr-5 font-medium">Active ancillary in block</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const block = periodToBlock(r.period);
                const anc = ancByBlock.get(block) ?? { high: [], low: [] };
                const isActive = r.net_mw !== 0;
                return (
                  <tr
                    key={r.period}
                    className={`border-b border-bg-border/40 hover:bg-bg-elevated/60 transition-colors ${
                      isActive ? "" : "text-fg-subtle"
                    }`}
                  >
                    <td className="py-2 px-5 text-fg-muted">{r.period}</td>
                    <td className="py-2 text-fg-muted">{r.time}</td>
                    <td className="py-2 text-fg-subtle text-xs whitespace-nowrap">{blockWindow(block)}</td>
                    <td className="text-right py-2 text-brand">{r.pd_mw > 0 ? r.pd_mw.toFixed(0) : "—"}</td>
                    <td className="text-right py-2 text-accent-amber">{r.pc_mw > 0 ? r.pc_mw.toFixed(0) : "—"}</td>
                    <td className={`text-right py-2 font-semibold ${
                      r.net_mw > 0 ? "text-brand" : r.net_mw < 0 ? "text-accent-amber" : "text-fg-subtle"
                    }`}>
                      {r.net_mw !== 0 ? (r.net_mw > 0 ? "+" : "") + r.net_mw.toFixed(0) : "0"}
                    </td>
                    <td className="text-right py-2 text-fg">
                      {r.da_pos_mw !== 0 ? (r.da_pos_mw > 0 ? "+" : "") + r.da_pos_mw.toFixed(0) : "0"}
                    </td>
                    <td className="text-right py-2 text-accent-teal">{r.soc_pct.toFixed(1)}</td>
                    <td className="py-2 pr-5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {anc.high.length === 0 && anc.low.length === 0 && (
                          <span className="text-fg-subtle text-[11px]">—</span>
                        )}
                        {anc.high.map((label) => (
                          <span
                            key={label}
                            className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-brand-light text-brand-dark border border-brand/20"
                          >
                            {label}
                          </span>
                        ))}
                        {anc.low.map((label) => (
                          <span
                            key={label}
                            className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-accent-gold/12 text-accent-gold border border-accent-gold/25"
                          >
                            {label}
                          </span>
                        ))}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
