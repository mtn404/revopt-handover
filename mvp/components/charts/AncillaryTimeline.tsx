"use client";

import { cn } from "@/lib/utils";
import type { AncillaryProduct } from "@/lib/data";

const HIGH_COLOUR = "#2E6EE8";   // Utilidex blue (discharge family)
const LOW_COLOUR  = "#F0AB36";   // amber (charge family)
// EFA-day block boundaries (NESO 23:00 → 23:00 UTC EFA day)
const BLOCKS = [
  { id: 1, label: "23:00–03:00" },
  { id: 2, label: "03:00–07:00" },
  { id: 3, label: "07:00–11:00" },
  { id: 4, label: "11:00–15:00" },
  { id: 5, label: "15:00–19:00" },
  { id: 6, label: "19:00–23:00" },
];

/**
 * Multi-product ancillary timeline. Each row is one of the 7 NESO ancillary
 * products. Each cell is a 4-hour EFA block (6 per day). Cells with MW > 0
 * are highlighted (sea-blue for HIGH-direction, amber for LOW-direction).
 *
 * Below the per-product rows we also show the SUM across all HIGH products
 * and across all LOW products per block — this is what enforces the LP's
 *   pd_v[t] + sum(HIGH) ≤ P_MAX
 *   pc_v[t] + sum(LOW)  ≤ P_MAX
 * capacity sharing constraint.
 */
export function AncillaryTimeline({
  products,
  pMaxMw = 50,
}: {
  products: AncillaryProduct[];
  pMaxMw?: number;
}) {
  // Compute per-block totals split HIGH / LOW
  const highTotals = BLOCKS.map((b) =>
    products
      .filter((p) => p.direction === "high")
      .reduce((a, p) => a + (p.blocks.find((bl) => bl.block === b.id)?.mw ?? 0), 0)
  );
  const lowTotals = BLOCKS.map((b) =>
    products
      .filter((p) => p.direction === "low")
      .reduce((a, p) => a + (p.blocks.find((bl) => bl.block === b.id)?.mw ?? 0), 0)
  );

  return (
    <div className="overflow-x-auto">
      <div className="min-w-[680px]">
        {/* Header: 4-hour EFA block windows */}
        <div className="grid grid-cols-[140px_repeat(6,minmax(0,1fr))] gap-2 mb-2 pb-2 border-b border-bg-border">
          <div className="text-[11px] uppercase text-fg-muted tracking-wider font-medium">
            Product · Direction
          </div>
          {BLOCKS.map((b) => (
            <div key={b.id} className="text-[11px] uppercase text-fg-muted tracking-wider font-medium text-center">
              <div>Block {b.id}</div>
              <div className="text-[9px] text-fg-subtle font-normal normal-case tracking-normal">
                {b.label}
              </div>
            </div>
          ))}
        </div>

        {/* Product rows */}
        <div className="space-y-1.5">
          {products.map((p) => {
            const colour = p.direction === "high" ? HIGH_COLOUR : LOW_COLOUR;
            return (
              <div
                key={p.product}
                className="grid grid-cols-[140px_repeat(6,minmax(0,1fr))] gap-2 items-center"
              >
                {/* Product label */}
                <div className="flex items-center gap-2 text-xs">
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ backgroundColor: colour }}
                  />
                  <span className="font-medium text-fg">{p.product}</span>
                  <span className="text-fg-subtle text-[10px] uppercase tracking-wider">
                    {p.direction === "high" ? "DISCH" : "CHG"}
                  </span>
                </div>

                {/* 6 EFA block cells */}
                {BLOCKS.map((b) => {
                  const blk = p.blocks.find((bl) => bl.block === b.id);
                  const mw = blk?.mw ?? 0;
                  const active = mw > 0;
                  const widthPct = Math.min((mw / pMaxMw) * 100, 100);
                  return (
                    <div
                      key={b.id}
                      className={cn(
                        "h-8 rounded relative overflow-hidden border",
                        active ? "border-transparent" : "border-bg-border bg-bg-elevated/40"
                      )}
                      title={
                        active
                          ? `${p.product} · Block ${b.id} (${b.label}) · ${mw} MW @ £${blk?.price.toFixed(2)}/MW·h`
                          : `${p.product} · Block ${b.id}: no commitment`
                      }
                    >
                      {active && (
                        <>
                          <div
                            className="absolute inset-y-0 left-0 rounded"
                            style={{ width: `${widthPct}%`, backgroundColor: colour, opacity: 0.22 }}
                          />
                          <div
                            className="absolute inset-y-0 left-0"
                            style={{
                              width: `${widthPct}%`,
                              backgroundImage: `linear-gradient(90deg, ${colour} 0%, ${colour}cc 100%)`,
                              opacity: 0.78,
                            }}
                          />
                          <div className="absolute inset-0 flex items-center justify-between px-2 text-[11px] font-medium text-white tabular">
                            <span>{mw} MW</span>
                            <span className="opacity-90">£{blk?.price.toFixed(1)}</span>
                          </div>
                        </>
                      )}
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>

        {/* Totals row — HIGH / LOW capacity utilisation per block */}
        <div className="mt-3 pt-3 border-t border-bg-border">
          <div className="grid grid-cols-[140px_repeat(6,minmax(0,1fr))] gap-2 items-center">
            <div className="text-xs font-semibold text-fg">∑ HIGH (disch budget)</div>
            {highTotals.map((tot, i) => (
              <div
                key={i}
                className={cn(
                  "h-7 rounded flex items-center justify-center text-[11px] font-semibold tabular",
                  tot > pMaxMw
                    ? "bg-accent-red/15 text-accent-red"
                    : tot > 0
                    ? "bg-brand-light text-brand-dark"
                    : "bg-bg-elevated text-fg-subtle"
                )}
              >
                {tot} / {pMaxMw} MW
              </div>
            ))}
          </div>
          <div className="grid grid-cols-[140px_repeat(6,minmax(0,1fr))] gap-2 items-center mt-1.5">
            <div className="text-xs font-semibold text-fg">∑ LOW (chg budget)</div>
            {lowTotals.map((tot, i) => (
              <div
                key={i}
                className={cn(
                  "h-7 rounded flex items-center justify-center text-[11px] font-semibold tabular",
                  tot > pMaxMw
                    ? "bg-accent-red/15 text-accent-red"
                    : tot > 0
                    ? "bg-accent-gold/15 text-accent-gold"
                    : "bg-bg-elevated text-fg-subtle"
                )}
              >
                {tot} / {pMaxMw} MW
              </div>
            ))}
          </div>
        </div>

        <p className="mt-3 text-[11px] text-fg-muted leading-relaxed">
          Multiple ancillary products <em>can</em> be committed simultaneously in the same EFA
          block — they share the discharge capacity budget (HIGH family: DC-H, DM-H, DR-H, FFR)
          or the charge capacity budget (LOW family: DC-L, DM-L, DR-L). The LP respects:
          <code className="ml-1 text-fg bg-bg-elevated px-1.5 py-0.5 rounded tabular text-[10px]">
            pd[t] + Σ HIGH ≤ P_max
          </code>{" "}
          and{" "}
          <code className="text-fg bg-bg-elevated px-1.5 py-0.5 rounded tabular text-[10px]">
            pc[t] + Σ LOW ≤ P_max
          </code>{" "}
          for every half-hour. The total rows above show how much of each direction&apos;s 50 MW
          budget is allocated to ancillary, leaving the rest for wholesale dispatch.
        </p>
      </div>
    </div>
  );
}
