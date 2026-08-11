"use client";

import {
  ComposedChart, Bar, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from "recharts";

type Day = {
  date: string; label: string;
  da_mean: number; da_peak: number; da_trough: number;
  sbp_mean: number; sbp_peak: number;
  spike_periods: number;
};

const BRAND   = "#2E6EE8";
const GOLD    = "#E8A33D";
const RED     = "#E54848";
const PURPLE  = "#7C3AED";

export function WeeklyForecast({ data }: { data: Day[] }) {
  return (
    <ResponsiveContainer width="100%" height={300}>
      <ComposedChart data={data} margin={{ top: 8, right: 24, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="label" tick={{ fontSize: 11 }} />
        <YAxis yAxisId="left" tick={{ fontSize: 11 }} label={{ value: "£/MWh", angle: -90, position: "insideLeft", offset: 8, fill: "#5A6B7D", fontSize: 11 }} />
        <YAxis yAxisId="right" orientation="right" tick={{ fontSize: 11 }} domain={[0, 8]} label={{ value: "# spikes", angle: 90, position: "insideRight", offset: 0, fill: "#5A6B7D", fontSize: 11 }} />
        <Tooltip />
        <Legend wrapperStyle={{ fontSize: 11, paddingTop: 8 }} />
        <Bar yAxisId="left" dataKey="da_mean" name="DA mean" fill={BRAND} />
        <Line yAxisId="left" dataKey="da_peak" name="DA peak" stroke={GOLD} strokeWidth={2.2} dot={{ r: 3 }} />
        <Line yAxisId="left" dataKey="sbp_peak" name="SBP peak" stroke={RED} strokeWidth={1.5} strokeDasharray="4 3" dot={{ r: 2 }} />
        <Line yAxisId="right" dataKey="spike_periods" name="Spike periods" stroke={PURPLE} strokeWidth={2} dot={{ r: 3 }} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
