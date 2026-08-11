"use client";

import { cn } from "@/lib/utils";
import type { AncillaryProduct } from "@/lib/data";

const HIGH_COLOUR = "#2E6EE8";   // Utilidex blue — discharge family
const LOW_COLOUR  = "#F0AB36";   // amber — charge family

// EFA-day block boundaries (NESO 23:00 → 23:00 UTC EFA day)
const BLOCKS = [
  { id: 1, label: "23–03" },
  { id: 2, label: "03–07" },
  { id: 3, label: "07–11" },
  { id: 4, label: "11–15" },
  { id: 5, label: "15–19" },
  { id: 6, label: "19–23" },
];

/**
 * Compact, dashboard-friendly summary of ancillary commitments per 4-hour
 * EFA block. Six block-columns; each lists the products committed in that
 * block as small coloured chips with MW alongside.
 */
export function CompactAncillaryStrip({
  products,
}: {
  products: AncillaryProduct[];
}) {
  return (
    <div className="grid grid-cols-6 gap-2">
      {BLOCKS.map((b) => {
        const active = products
          .map((p) => ({
            product: p.product,
            direction: p.direction,
            mw: p.blocks.find((bl) => bl.block === b.id)?.mw ?? 0,
          }))
          .filter((x) => x.mw > 0);

        const totalHigh = active.filter((a) => a.direction === "high").reduce((s, a) => s + a.mw, 0);
        const totalLow  = active.filter((a) => a.direction === "low").reduce((s, a) => s + a.mw, 0);

        return (
          <div
            key={b.id}
            className={cn(
              "rounded-md border p-2 min-h-[68px]",
              active.length > 0
                ? "border-bg-border bg-bg-surface"
                : "border-bg-border bg-bg-elevated/40 opacity-60"
            )}
          >
            <div className="flex items-center justify-between mb-1.5">
              <div className="text-[10px] uppercase text-fg-muted tracking-wider">
                B{b.id} · {b.label}
              </div>
              {active.length > 0 && (
                <div className="flex items-center gap-1 text-[10px] text-fg-subtle tabular">
                  {totalHigh > 0 && (
                    <span style={{ color: HIGH_COLOUR }} className="font-semibold">
                      ↑{totalHigh}
                    </span>
                  )}
                  {totalLow > 0 && (
                    <span style={{ color: LOW_COLOUR }} className="font-semibold">
                      ↓{totalLow}
                    </span>
                  )}
                </div>
              )}
            </div>
            <div className="flex flex-wrap gap-1">
              {active.length === 0 ? (
                <span className="text-[10px] text-fg-subtle">No anc</span>
              ) : (
                active.map((a) => (
                  <span
                    key={a.product}
                    className={cn(
                      "inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium tabular border"
                    )}
                    style={{
                      color: a.direction === "high" ? HIGH_COLOUR : LOW_COLOUR,
                      borderColor: (a.direction === "high" ? HIGH_COLOUR : LOW_COLOUR) + "40",
                      backgroundColor: (a.direction === "high" ? HIGH_COLOUR : LOW_COLOUR) + "12",
                    }}
                  >
                    {a.product} {a.mw}
                  </span>
                ))
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
