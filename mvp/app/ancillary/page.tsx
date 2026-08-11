"use client";

import { Card, Badge } from "@/components/Card";
import { CopyButton } from "@/components/CopyButton";
import { getSnapshot } from "@/lib/data";
import { cn, fmtGBP } from "@/lib/utils";
import { useAsset, scaleMW } from "@/lib/asset-context";

export default function AncillaryPage() {
  const s = getSnapshot();
  const { spec } = useAsset();

  // Scale MW values for the user-selected asset
  const products = s.ancillary_bids_today.map((p) => ({
    ...p,
    blocks: p.blocks.map((b) => ({ ...b, mw: Math.round(scaleMW(b.mw, spec)) })),
  }));

  const csvPayload = [
    "product,block,window,mw,price_gbp_per_mw_per_h",
    ...products.flatMap((p) =>
      p.blocks.map((b) => `${p.product},${b.block},${b.window},${b.mw},${b.price}`)
    ),
  ].join("\n");

  const productTotals = products.map((p) => {
    const total_mwh_committed = p.blocks.reduce((a, b) => a + b.mw * 4, 0);
    const expected_revenue = p.blocks.reduce((a, b) => a + b.mw * b.price * 4, 0);
    return { ...p, total_mwh_committed, expected_revenue };
  });

  const totalRevenue = productTotals.reduce((a, p) => a + p.expected_revenue, 0);
  const totalActiveBlocks = products.reduce(
    (a, p) => a + p.blocks.filter((b) => b.mw > 0).length, 0
  );
  const totalBlocks = products.reduce((a, p) => a + p.blocks.length, 0);

  const dispatchDateStr = s.freshness?.dispatch_date ?? s.freshness?.last_data_through ?? "";
  const dispatchDateObj = dispatchDateStr ? new Date(dispatchDateStr + "T12:00:00Z") : new Date();
  const dispatchDateLabel = dispatchDateStr
    ? dispatchDateObj.toLocaleDateString("en-GB", {
        weekday: "long", day: "numeric", month: "long", year: "numeric",
      })
    : "";
  const dispatchRelative = (() => {
    if (!dispatchDateStr) return "Ancillary bids";
    const today = new Date(); today.setUTCHours(12, 0, 0, 0);
    const diff = Math.round((dispatchDateObj.getTime() - today.getTime()) / 86400000);
    if (diff === 0)  return "Today's ancillary bids";
    if (diff === 1)  return "Tomorrow's ancillary bids";
    if (diff === -1) return "Yesterday's ancillary bids";
    return diff > 1 ? `Ancillary bids (in ${diff} days)` : `Ancillary bids (${-diff} days ago)`;
  })();

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-fg tracking-tight">
          {dispatchRelative} &mdash; {dispatchDateLabel}
        </h2>
        <p className="text-sm text-fg-muted mt-1">
          Recommended bid volumes per NESO Enduring Auction Capability (EAC) product, by 4-hour
          EFA block, for the selected{" "}
          <span className="font-semibold text-fg">{spec.power_mw} MW · {spec.power_mw * spec.duration_h} MWh ({spec.duration_h}h)</span>{" "}
          battery. Blocks align to NESO&apos;s EFA-day boundary (23:00 &rarr; 23:00 UTC) so the
          CSV pastes directly into the EAC portal. Submission deadline is 09:00 UTC gate closure on the day of delivery.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card>
          <div className="text-[11px] uppercase text-fg-muted tracking-wider">Expected ancillary revenue</div>
          <div className="text-2xl font-semibold text-brand tabular mt-1">{fmtGBP(totalRevenue)}</div>
          <div className="text-[11px] text-fg-muted mt-1">Across {totalActiveBlocks} active blocks today</div>
        </Card>
        <Card>
          <div className="text-[11px] uppercase text-fg-muted tracking-wider">Block coverage</div>
          <div className="text-2xl font-semibold text-fg tabular mt-1">{totalActiveBlocks} / {totalBlocks}</div>
          <div className="text-[11px] text-fg-muted mt-1">Active blocks (6 products × 6 blocks)</div>
        </Card>
        <Card>
          <div className="text-[11px] uppercase text-fg-muted tracking-wider">Gate closure</div>
          <div className="text-2xl font-semibold text-accent-gold tabular mt-1">09:00 UTC</div>
          <div className="text-[11px] text-fg-muted mt-1">Submit to NESO EAC portal</div>
        </Card>
      </div>

      {productTotals.map((p) => (
        <Card
          key={p.product}
          title={`${p.product} — ${p.name}`}
          subtitle={`Direction: ${p.direction.toUpperCase()} · ${p.blocks.filter(b => b.mw > 0).length}/6 blocks committed · Expected revenue ${fmtGBP(p.expected_revenue)}`}
          action={
            <Badge variant={p.direction === "low" ? "gold" : "brand"}>
              {p.direction === "low" ? "Charge service" : "Discharge service"}
            </Badge>
          }
        >
          <div className="grid grid-cols-6 gap-3">
            {p.blocks.map((b) => (
              <div
                key={b.block}
                className={cn(
                  "rounded-lg border p-3 transition-colors",
                  b.mw > 0
                    ? "border-brand/40 bg-brand-light"
                    : "border-bg-border bg-bg-elevated/60 opacity-60"
                )}
              >
                <div className="text-[10px] uppercase text-fg-muted tracking-wider">
                  Block {b.block}
                </div>
                <div className="text-[10px] text-fg-subtle mt-0.5">{b.window}</div>
                <div className="mt-2 text-lg font-semibold text-fg tabular">
                  {b.mw} MW
                </div>
                <div className="text-[10px] text-fg-muted tabular mt-0.5">
                  @ £{b.price.toFixed(2)}/MW·h
                </div>
              </div>
            ))}
          </div>
        </Card>
      ))}

      <Card
        title="Export — copy to NESO EAC portal"
        subtitle="CSV-formatted bid payload for direct paste"
        action={<CopyButton text={csvPayload} label="Copy CSV" />}
      >
        <pre className="text-[11px] tabular bg-bg-elevated border border-bg-border rounded-lg p-4 overflow-x-auto text-fg-muted leading-relaxed">
{csvPayload}
        </pre>
      </Card>
    </div>
  );
}
