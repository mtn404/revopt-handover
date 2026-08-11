"use client";

import {
  ComposedChart, Bar, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, Cell,
} from "recharts";

type DailyRow = { date: string; weekday: string; realised: number; pf: number; da?: number };
const BRAND = "#2E6EE8";
const GOLD  = "#E8A33D";
const TEXT_MUTED = "#5A6B7D";

/**
 * Bars: realised LP gross (£) per day · Outline bars: PF v6 oracle gross (£)
 * Weekends are visually dimmed so the operator can immediately scan
 * weekday-vs-weekend revenue patterns.
 */
export function DailyRevenueChart({
  data,
  height = 280,
  hideLegend = false,
}: {
  data: DailyRow[];
  height?: number;
  hideLegend?: boolean;
}) {
  // Build a chart-friendly series — shorten the date label to DD/MM
  const series = data.map((d) => ({
    label: d.date.slice(8) + "/" + d.date.slice(5, 7),
    weekday: d.weekday,
    realised: d.realised,
    pf: d.pf,
    pctPF: d.pf > 0 ? (d.realised / d.pf) * 100 : 0,
    isWeekend: d.weekday === "Sat" || d.weekday === "Sun",
  }));

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={series} margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis
          dataKey="label"
          tick={{ fontSize: 10 }}
          interval={1}
          tickMargin={6}
        />
        <YAxis
          tick={{ fontSize: 11 }}
          tickFormatter={(v) => `£${(v / 1000).toFixed(0)}k`}
          label={{ value: "GBP/day", angle: -90, position: "insideLeft", offset: 8, fill: TEXT_MUTED, fontSize: 11 }}
        />
        <Tooltip
          formatter={(v: number, name) => {
            if (name === "% PF") return [`${v.toFixed(1)}%`, name];
            return [`£${v.toLocaleString()}`, name];
          }}
          labelFormatter={(label, payload) => {
            const w = payload?.[0]?.payload?.weekday;
            return w ? `${label} · ${w}` : label;
          }}
        />
        {!hideLegend && <Legend wrapperStyle={{ fontSize: 11, paddingTop: 6 }} />}
        <Bar dataKey="pf" name="PF oracle" fill={GOLD} fillOpacity={0.28} />
        <Bar dataKey="realised" name="LP realised">
          {series.map((d, i) => (
            <Cell key={i} fill={BRAND} fillOpacity={d.isWeekend ? 0.55 : 1} />
          ))}
        </Bar>
      </ComposedChart>
    </ResponsiveContainer>
  );
}
