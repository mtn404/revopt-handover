import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function fmtGBP(v: number, opts?: { compact?: boolean; signed?: boolean }) {
  const sign = opts?.signed && v > 0 ? "+" : "";
  if (opts?.compact && Math.abs(v) >= 1_000_000) return `${sign}£${(v / 1_000_000).toFixed(2)}M`;
  if (opts?.compact && Math.abs(v) >= 1_000) return `${sign}£${(v / 1_000).toFixed(1)}k`;
  return `${sign}£${v.toLocaleString("en-GB", { maximumFractionDigits: 0 })}`;
}

export function fmtMW(v: number, digits = 1) {
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)} MW`;
}

export function fmtPct(v: number, digits = 1) {
  return `${v.toFixed(digits)}%`;
}

export function periodToTime(period: number): string {
  // Settlement period 1 = 00:00–00:30 UTC
  const minutes = (period - 1) * 30;
  const h = Math.floor(minutes / 60).toString().padStart(2, "0");
  const m = (minutes % 60).toString().padStart(2, "0");
  return `${h}:${m}`;
}
