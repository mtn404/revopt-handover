"use client";

import Link from "next/link";
import { Battery, Settings as SettingsIcon } from "lucide-react";
import { useAsset } from "@/lib/asset-context";

/**
 * Read-only summary of the currently-selected battery configuration.
 * Editing happens on the /settings page (see app/settings/page.tsx).
 * Shown in the top-right of the topbar.
 */
export function AssetSelector() {
  const { spec } = useAsset();
  const energyMWh = spec.power_mw * spec.duration_h;

  return (
    <Link
      href="/settings"
      title="Edit on the Settings page"
      className="group flex items-center gap-2 px-3 py-1.5 rounded-md text-xs font-medium border bg-white/15 hover:bg-white/25 text-white border-white/20 transition-colors"
    >
      <Battery className="h-3.5 w-3.5" />
      <span className="tabular">
        {spec.power_mw} MW · {energyMWh} MWh ({spec.duration_h}h)
      </span>
      <SettingsIcon className="h-3.5 w-3.5 opacity-60 group-hover:opacity-100" />
    </Link>
  );
}
