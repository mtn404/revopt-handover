"use client";

export type SpikeCell = { period: number; time: string; prob: number };

// Probability ≥ this gets the "spike-predicted" highlight (ring + saturated text).
// Set to match the typical per-month F1-tuned classification boundary so the
// heatmap visually agrees with how the model itself flags spike-likely periods.
const SPIKE_PRED_THRESHOLD = 0.10;

function colorForProb(p: number): string {
  // Compressed ramp — the action happens between 0% and ~20%, since the model
  // operates on a ~5% base rate. Above 20% is treated as "very high concern".
  if (p < 0.02) return "rgba(46,110,232,0.06)";    // baseline tint
  if (p < 0.04) return "rgba(46,110,232,0.18)";    // mild
  if (p < 0.06) return "rgba(232,163,61,0.30)";    // touching concern
  if (p < 0.08) return "rgba(232,163,61,0.50)";    // moderate
  if (p < 0.10) return "rgba(232,163,61,0.70)";    // elevated
  if (p < 0.15) return "rgba(229,72,72,0.65)";     // spike-predicted (low end)
  if (p < 0.25) return "rgba(229,72,72,0.82)";     // spike-predicted (mid)
  return "rgba(229,72,72,0.95)";                   // spike-predicted (high)
}

export function SpikeHeatmap({ data }: { data: SpikeCell[] }) {
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-12 gap-1">
        {data.map((c) => {
          const isSpikePred = c.prob >= SPIKE_PRED_THRESHOLD;
          const isHot = c.prob >= 0.08;
          return (
            <div
              key={c.period}
              className={`aspect-square rounded flex items-center justify-center text-[9px] tabular relative ${
                isHot ? "text-white font-semibold" : "text-fg"
              } ${isSpikePred ? "ring-2 ring-accent-red ring-offset-1 ring-offset-bg-surface" : ""}`}
              style={{ backgroundColor: colorForProb(c.prob) }}
              title={`${c.time}: ${(c.prob * 100).toFixed(1)}% spike probability${
                isSpikePred ? " — spike predicted" : ""
              }`}
            >
              {c.prob >= 0.03 ? `${(c.prob * 100).toFixed(0)}%` : ""}
            </div>
          );
        })}
      </div>
      <div className="flex items-center gap-3 text-[10px] text-fg-muted">
        <span>0%</span>
        <div
          className="h-2 flex-1 rounded"
          style={{
            background:
              "linear-gradient(90deg, rgba(46,110,232,0.10), rgba(232,163,61,0.55), rgba(229,72,72,0.95))",
          }}
        />
        <span>&gt;20%</span>
        <span className="inline-flex items-center gap-1 ml-2">
          <span className="inline-block w-3 h-3 rounded ring-2 ring-accent-red bg-accent-red/60" />
          <span>spike predicted (≥ {(SPIKE_PRED_THRESHOLD * 100).toFixed(0)}%)</span>
        </span>
        <span className="text-fg-subtle ml-2">
          Each cell = 30-min SP (00:00 → 23:30 UTC, left → right)
        </span>
      </div>
    </div>
  );
}
