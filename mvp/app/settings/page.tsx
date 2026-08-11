"use client";

import { Card, Badge } from "@/components/Card";
import { getSnapshot } from "@/lib/data";
import { useAsset, POWER_OPTIONS, DURATION_OPTIONS, REFERENCE_SPEC, type AssetSpec } from "@/lib/asset-context";
import { DUOS_REGIONS, DUOS_VOLTAGE_LABELS, type DuosRegionId, type DuosVoltage } from "@/lib/duos";
import { cn } from "@/lib/utils";
import { RotateCw } from "lucide-react";

export default function SettingsPage() {
  const s = getSnapshot();
  const { spec, setSpec } = useAsset();
  const energyMWh = spec.power_mw * spec.duration_h;
  const isReference =
    spec.power_mw === REFERENCE_SPEC.power_mw &&
    spec.duration_h === REFERENCE_SPEC.duration_h &&
    spec.rte === REFERENCE_SPEC.rte &&
    spec.region === REFERENCE_SPEC.region &&
    spec.voltage === REFERENCE_SPEC.voltage;

  const tariff = DUOS_REGIONS[spec.region]?.tariff.rates[spec.voltage];

  const update = (patch: Partial<AssetSpec>) => setSpec({ ...spec, ...patch });

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-fg tracking-tight">Asset &amp; System Settings</h2>
          <p className="text-sm text-fg-muted mt-1">
            All battery and network-charge configuration lives here. The top-bar tile is read-only
            and links back to this page. All dashboard, dispatch, ancillary and revenue figures
            scale automatically when you change these.
          </p>
        </div>
        {!isReference && (
          <button
            onClick={() => setSpec(REFERENCE_SPEC)}
            className="text-[11px] uppercase tracking-wider text-brand hover:text-brand-dark flex items-center gap-1.5 border border-bg-border rounded-md px-3 py-1.5 hover:bg-bg-elevated transition-colors"
          >
            <RotateCw className="h-3 w-3" />
            Reset to reference (50 MW · 2h)
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">

        {/* ============================================ */}
        {/* Battery configuration — interactive form     */}
        {/* ============================================ */}
        <Card title="Battery configuration" action={<Badge variant="brand">Live</Badge>}>
          <div className="space-y-5">
            {/* Rated power — continuous slider 1–500 MW + preset chips */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <div className="text-[11px] uppercase text-fg-muted tracking-wider">
                  Rated power
                </div>
                <div className="text-sm tabular text-fg font-semibold">{spec.power_mw} MW</div>
              </div>
              <input
                type="range"
                min={1}
                max={500}
                step={1}
                value={spec.power_mw}
                onChange={(e) => update({ power_mw: parseInt(e.target.value, 10) })}
                className="w-full accent-brand"
                aria-label="Rated power in MW"
              />
              <div className="flex items-center justify-between text-[10px] text-fg-subtle tabular mt-1">
                <span>1 MW</span>
                <span>250 MW</span>
                <span>500 MW</span>
              </div>
              <div className="grid grid-cols-5 gap-1.5 mt-3">
                {POWER_OPTIONS.map((mw) => (
                  <button
                    key={mw}
                    onClick={() => update({ power_mw: mw })}
                    className={cn(
                      "h-8 rounded text-[11px] font-medium tabular transition-colors border",
                      spec.power_mw === mw
                        ? "bg-brand text-white border-brand"
                        : "bg-bg-elevated text-fg-muted border-bg-border hover:border-brand/40 hover:text-fg"
                    )}
                  >
                    {mw} MW
                  </button>
                ))}
              </div>
              <div className="text-[10px] text-fg-subtle mt-2 leading-relaxed">
                Drag for any size, or tap a preset. Every dashboard figure rescales linearly to the
                selected nameplate power.
              </div>
            </div>

            {/* Storage duration */}
            <div>
              <div className="text-[11px] uppercase text-fg-muted tracking-wider mb-2">
                Storage duration
              </div>
              <div className="grid grid-cols-3 gap-1.5">
                {DURATION_OPTIONS.map((h) => (
                  <button
                    key={h}
                    onClick={() => update({ duration_h: h })}
                    className={cn(
                      "h-9 rounded text-xs font-medium tabular transition-colors border",
                      spec.duration_h === h
                        ? "bg-brand text-white border-brand"
                        : "bg-bg-elevated text-fg-muted border-bg-border hover:border-brand/40 hover:text-fg"
                    )}
                  >
                    {h}h
                  </button>
                ))}
              </div>
            </div>

            {/* RTE */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <div className="text-[11px] uppercase text-fg-muted tracking-wider">
                  Round-trip efficiency
                </div>
                <div className="text-xs tabular text-fg">{(spec.rte * 100).toFixed(0)}%</div>
              </div>
              <input
                type="range" min={0.80} max={0.95} step={0.01}
                value={spec.rte}
                onChange={(e) => update({ rte: parseFloat(e.target.value) })}
                className="w-full accent-brand"
              />
            </div>

            {/* SoC carry-in — read-only (sourced from the dispatch parquet) */}
            <div className="bg-bg-elevated rounded p-3 border border-bg-border">
              <div className="flex items-center justify-between text-[11px] uppercase text-fg-muted tracking-wider mb-1">
                <span>SoC carry-in (from yesterday)</span>
                <Badge>auto</Badge>
              </div>
              <div className="text-xl font-semibold text-accent-teal tabular mt-1">
                {(s.asset.soc_carry_in_pct ?? s.asset.soc_start_pct ?? 50).toFixed(0)}%
              </div>
              <div className="text-[10px] text-fg-subtle mt-2 leading-relaxed">
                Read from the LP dispatch parquet — this is the actual end-of-day SoC
                from the day before the dashboard&apos;s &quot;today&quot;. The LP rolls SoC forward
                every day, so this updates with each weekly refresh.
              </div>
            </div>

            <div className="bg-bg-elevated rounded p-3 text-[11px] text-fg-muted border border-bg-border space-y-1">
              <div className="flex items-center justify-between">
                <span>Energy capacity</span>
                <span className="text-fg font-semibold tabular">{energyMWh} MWh</span>
              </div>
              <div className="flex items-center justify-between">
                <span>C-rate</span>
                <span className="text-fg tabular">{(1 / spec.duration_h).toFixed(2)} C</span>
              </div>
              <div className="flex items-center justify-between">
                <span>Cycle limit</span>
                <span className="text-fg tabular">2.0 / day</span>
              </div>
              <div className="flex items-center justify-between">
                <span>SoC bounds (LP constraint)</span>
                <span className="text-fg tabular">10% — 95%</span>
              </div>
              <div className="flex items-center justify-between">
                <span>Degradation cost</span>
                <span className="text-fg tabular">£5 / MWh throughput</span>
              </div>
            </div>

            <div className="text-[10px] text-fg-subtle leading-relaxed">
              Displayed MW figures scale linearly with rated power. Per-MW revenue scales with both
              rated power (linear) and duration (per sensitivity-sweep: £134k/£148k/£160k per MW/year
              for 1h/2h/4h respectively). Re-optimisation against the exact spec requires re-running
              the LP — production deployment would do this weekly.
            </div>
          </div>
        </Card>

        {/* ============================================ */}
        {/* DUoS region + voltage — interactive          */}
        {/* ============================================ */}
        <Card title="Network charges (DUoS)" action={<Badge variant="brand">Live</Badge>}>
          <div className="space-y-4">
            <div>
              <div className="text-[11px] uppercase text-fg-muted tracking-wider mb-2">
                Distribution region
              </div>
              <select
                value={spec.region}
                onChange={(e) => update({ region: e.target.value as DuosRegionId })}
                className="w-full px-3 py-2 text-sm bg-bg-elevated border border-bg-border rounded text-fg focus:outline-none focus:border-brand"
              >
                {Object.entries(DUOS_REGIONS).map(([id, r]) => (
                  <option key={id} value={id}>{r.name}</option>
                ))}
              </select>
            </div>

            <div>
              <div className="text-[11px] uppercase text-fg-muted tracking-wider mb-2">
                Voltage level
              </div>
              <div className="grid grid-cols-3 gap-1.5">
                {(["lv","hv","ehv"] as DuosVoltage[]).map((v) => (
                  <button
                    key={v}
                    onClick={() => update({ voltage: v })}
                    className={cn(
                      "h-9 rounded text-xs font-medium tabular transition-colors border px-2",
                      spec.voltage === v
                        ? "bg-brand text-white border-brand"
                        : "bg-bg-elevated text-fg-muted border-bg-border hover:border-brand/40 hover:text-fg"
                    )}
                  >
                    {DUOS_VOLTAGE_LABELS[v]}
                  </button>
                ))}
              </div>
            </div>

            {/* DUoS unit-rate table for current selection */}
            <div className="bg-bg-elevated rounded p-3 border border-bg-border text-xs">
              <div className="text-[10px] uppercase text-fg-muted tracking-wider mb-2">
                CDCM unit rates · {DUOS_REGIONS[spec.region].name} · {DUOS_VOLTAGE_LABELS[spec.voltage]}
              </div>
              <div className="grid grid-cols-3 gap-2 tabular">
                {tariff && (
                  <>
                    <div className="text-center">
                      <div className="text-fg-muted text-[10px] uppercase">Red</div>
                      <div className="text-fg font-semibold mt-0.5">£{tariff.red.toFixed(4)}/kWh</div>
                    </div>
                    <div className="text-center">
                      <div className="text-fg-muted text-[10px] uppercase">Amber</div>
                      <div className="text-fg font-semibold mt-0.5">£{tariff.amber.toFixed(4)}/kWh</div>
                    </div>
                    <div className="text-center">
                      <div className="text-fg-muted text-[10px] uppercase">Green</div>
                      <div className="text-fg font-semibold mt-0.5">£{tariff.green.toFixed(4)}/kWh</div>
                    </div>
                  </>
                )}
              </div>
              <div className="text-[10px] text-fg-subtle mt-2 leading-relaxed">
                Bands: Red 16:00–19:00 winter weekdays · Amber 07:00–23:00 weekdays · Green
                everything else. Charged on energy imported (battery charging); export
                does not incur DUoS unit charge under typical tariffs.
              </div>
            </div>

            <div className="text-[11px] text-fg-muted leading-relaxed">
              The dashboard&apos;s headline tiles and the Revenue page banner use these rates
              to deduct DUoS from gross LP revenue. Changing the region/voltage here
              propagates everywhere.
            </div>
          </div>
        </Card>

        {/* ============================================ */}
        {/* Data feeds                                   */}
        {/* ============================================ */}
        <Card title="Data feeds" action={<Badge variant="brand">{s.freshness?.pipeline_mode === "live" ? "Live" : "Backtest"}</Badge>}>
          <div className="space-y-2 text-sm">
            {[
              { name: "Elexon BMRS — day-ahead MID prices",      automated: true },
              { name: "Elexon BMRS — SBP / system prices",       automated: true },
              { name: "Elexon BMRS — demand outturn",            automated: true },
              { name: "Elexon BMRS — BOALF (aggregated)",        automated: true },
              { name: "NESO — EAC ancillary clearings",          automated: true },
              { name: "NESO — DA wind + demand forecasts",       automated: true },
              { name: "Carbon Intensity API — generation mix",   automated: true },
              { name: "Spectron — NBP gas (proprietary)",        automated: false },
              { name: "ICE Endex — UKA carbon (proprietary)",    automated: false },
            ].map((f) => (
              <div key={f.name} className="flex items-center justify-between py-1.5 border-b border-bg-border/50 last:border-0">
                <div className="flex items-center gap-2">
                  <span className={`inline-block h-1.5 w-1.5 rounded-full ${f.automated ? "bg-brand" : "bg-accent-gold"}`} />
                  <span className="text-fg-muted text-xs">{f.name}</span>
                </div>
                <span className={`text-[10px] tabular ${f.automated ? "text-fg-subtle" : "text-accent-gold"}`}>
                  {f.automated ? "auto" : "manual upload"}
                </span>
              </div>
            ))}
            <div className="pt-2 mt-1 border-t border-bg-border text-[11px] text-fg-muted leading-relaxed">
              Last refresh: <span className="text-fg tabular">{s.freshness?.last_refreshed_utc?.slice(0, 16).replace("T", " ") ?? "—"} UTC</span>
              <br />Data through: <span className="text-fg tabular">{s.freshness?.last_data_through ?? "—"}</span>
            </div>
          </div>
        </Card>

        {/* ============================================ */}
        {/* Refresh schedule                             */}
        {/* ============================================ */}
        <Card title="Refresh schedule">
          <div className="space-y-3 text-sm">
            <div className="flex items-center justify-between py-2 border-b border-bg-border/50">
              <span className="text-fg-muted">Full pipeline (master + retrain + LP + PF)</span>
              <span className="text-fg">Sunday 02:00 UTC</span>
            </div>
            <div className="flex items-center justify-between py-2 border-b border-bg-border/50">
              <span className="text-fg-muted">Public data pull + freshness stamp</span>
              <span className="text-fg">Daily 08:30 UTC</span>
            </div>
            <div className="flex items-center justify-between py-2 border-b border-bg-border/50">
              <span className="text-fg-muted">Walk-forward retrain anchor</span>
              <span className="text-fg">Monthly (1st)</span>
            </div>
            <div className="flex items-center justify-between py-2">
              <span className="text-fg-muted">State persistence</span>
              <span className="text-fg">Supabase Storage (private)</span>
            </div>
            <div className="pt-2 mt-1 border-t border-bg-border text-[11px] text-fg-muted leading-relaxed">
              The dashboard updates with new dispatch + LP/PF results after each weekly
              run. The daily job keeps the &quot;Refreshed&quot; timestamp current and pulls fresh
              raw data so Sunday&apos;s run starts with a warm tail.
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
