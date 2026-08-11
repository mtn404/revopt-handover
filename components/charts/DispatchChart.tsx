"use client";

import {
  ComposedChart, Bar, Line, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine, ResponsiveContainer, Cell,
} from "recharts";
import type { DispatchPoint } from "@/lib/data";

const BRAND      = "#2E6EE8";   // Utilidex blue (discharge)
const AMBER      = "#F0AB36";   // amber (charge)
const TEAL       = "#14B8A6";   // SoC line
const TEXT_MUTED = "#5A6B7D";

export function DispatchChart({ data }: { data: DispatchPoint[] }) {
  const series = data.map((d) => ({
    time: d.time,
    net: d.net_mw,
    soc: d.soc_pct,
  }));
  return (
    <ResponsiveContainer width="100%" height={260}>
      <ComposedChart data={series} margin={{ top: 8, right: 36, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="time" interval={5} tick={{ fontSize: 11 }} tickMargin={6} />
        <YAxis yAxisId="left" tick={{ fontSize: 11 }} label={{ value: "MW", angle: -90, position: "insideLeft", offset: 10, fill: TEXT_MUTED, fontSize: 11 }} />
        <YAxis yAxisId="right" orientation="right" domain={[0, 100]} tick={{ fontSize: 11 }} label={{ value: "SoC %", angle: 90, position: "insideRight", offset: 8, fill: TEXT_MUTED, fontSize: 11 }} />
        <Tooltip />
        <ReferenceLine yAxisId="left" y={0} stroke="#E1E6ED" />
        <Bar yAxisId="left" dataKey="net" name="Net MW (+ disch / − charge)">
          {series.map((d, i) => (
            <Cell key={i} fill={d.net >= 0 ? BRAND : AMBER} />
          ))}
        </Bar>
        <Line yAxisId="right" dataKey="soc" name="SoC %" stroke={TEAL} strokeWidth={2.2} dot={false} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
