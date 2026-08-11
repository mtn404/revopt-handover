"use client";

import { Bell, Search, RefreshCw } from "lucide-react";
import { AssetSelector } from "./AssetSelector";
import snapshot from "@/data/snapshot.json";

export function Topbar() {
  const today = new Date();
  const dateLabel = today.toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  });

  // Pipeline-mode badge still uses the snapshot's freshness block; the
  // refresh / data-through / retrained timestamps are intentionally hidden.
  const fresh = (snapshot as { freshness?: {
    pipeline_mode: "backtest" | "live";
  }}).freshness;
  const modeLabel = fresh?.pipeline_mode === "live" ? "LIVE" : "BACKTEST";
  const modeColor = fresh?.pipeline_mode === "live"
    ? "bg-emerald-500/30 text-emerald-50 border-emerald-300/40"
    : "bg-amber-500/25 text-amber-50 border-amber-300/40";

  return (
    <header className="h-16 shrink-0 bg-brand text-white px-6 flex items-center justify-between shadow-card">
      <div>
        <h1 className="font-display text-[19px] font-bold tracking-tight leading-tight flex items-center gap-2">
          BESS Optimiser
          <span
            className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border tracking-wider ${modeColor}`}
            title={`Pipeline mode: ${fresh?.pipeline_mode ?? "unknown"}`}
          >
            {modeLabel}
          </span>
        </h1>
        <p className="text-[11px] text-white/70 leading-tight mt-0.5">
          {dateLabel}
        </p>
      </div>
      <div className="flex items-center gap-3">
        <div className="hidden xl:flex items-center gap-2 bg-white/15 rounded-md px-3 py-1.5 text-xs text-white/80 w-64 border border-white/20">
          <Search className="h-3.5 w-3.5" />
          <span>Search settlement period, product, day…</span>
        </div>
        <button className="flex items-center gap-2 text-xs text-white hover:text-white bg-white/15 hover:bg-white/25 px-3 py-1.5 rounded-md transition-colors border border-white/20">
          <RefreshCw className="h-3.5 w-3.5" />
          <span>Refresh</span>
        </button>
        <AssetSelector />
        <button className="text-white/80 hover:text-white p-2 rounded-md hover:bg-white/15 transition-colors">
          <Bell className="h-4 w-4" />
        </button>
      </div>
    </header>
  );
}
