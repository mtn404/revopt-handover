"use client";

import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import type { ForecastPoint } from "@/lib/data";

const BRAND = "#2E6EE8";
const TEXT_MUTED = "#5A6B7D";

export function DaPriceChart({ data, height = 220 }: { data: ForecastPoint[]; height?: number }) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="daFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={BRAND} stopOpacity={0.32} />
            <stop offset="100%" stopColor={BRAND} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis
          dataKey="time"
          interval={7}
          tick={{ fontSize: 11 }}
          tickMargin={6}
        />
        <YAxis tick={{ fontSize: 11 }} label={{ value: "£/MWh", angle: -90, position: "insideLeft", offset: 10, fill: TEXT_MUTED, fontSize: 11 }} />
        <Tooltip />
        <Area dataKey="price" stroke={BRAND} fill="url(#daFill)" strokeWidth={2.2} />
      </AreaChart>
    </ResponsiveContainer>
  );
}
