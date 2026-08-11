"use client";

import React, { createContext, useContext, useEffect, useState } from "react";
import type { DuosRegionId, DuosVoltage } from "./duos";

/**
 * User-selectable battery configuration.
 * Persisted to localStorage. All displayed MW / revenue figures scale
 * linearly within a duration class (per dissertation §5.4 sensitivity finding:
 * revenue scales approximately linearly in rated power within each duration).
 * Per-MW revenue varies with duration via the sensitivity-sweep multipliers.
 *
 * `region` + `voltage` drive DUoS deductions on the revenue side. They do
 * not change the LP itself (DUoS is applied post-LP — see lib/duos.ts).
 *
 * `soc_start_pct` is the carry-in state-of-charge at the beginning of the
 * displayed day. Used by the dashboard's State-of-charge plan card.
 */
export type AssetSpec = {
  power_mw: number;
  duration_h: 1 | 2 | 4;
  rte: number;
  region: DuosRegionId;
  voltage: DuosVoltage;
};

// From the dissertation sensitivity sweep (£/MW/year extrapolated):
const PER_MW_BY_DURATION: Record<1 | 2 | 4, number> = {
  1: 134407,
  2: 147736,
  4: 160405,
};

// Reference spec used when the LP was run for the baked snapshot.
export const REFERENCE_SPEC: AssetSpec = {
  power_mw: 50,
  duration_h: 2,
  rte: 0.88,
  region: "lpn",
  voltage: "hv",
};
const REFERENCE_PER_MW = PER_MW_BY_DURATION[REFERENCE_SPEC.duration_h];

/**
 * Scale a MW quantity from the reference spec (50 MW) to the user-selected
 * power. Used for dispatch magnitudes.
 */
export function scaleMW(referenceMW: number, spec: AssetSpec): number {
  return (referenceMW * spec.power_mw) / REFERENCE_SPEC.power_mw;
}

/**
 * Scale a revenue figure. Combines (a) linear scaling in MW with (b) the
 * duration multiplier from the sensitivity sweep.
 */
export function scaleRevenue(referenceGBP: number, spec: AssetSpec): number {
  const mwRatio = spec.power_mw / REFERENCE_SPEC.power_mw;
  const durationRatio = PER_MW_BY_DURATION[spec.duration_h] / REFERENCE_PER_MW;
  return referenceGBP * mwRatio * durationRatio;
}

// ---------------------------------------------------------------------------
// Context + hooks
// ---------------------------------------------------------------------------

const AssetContext = createContext<{
  spec: AssetSpec;
  setSpec: (s: AssetSpec) => void;
}>({
  spec: REFERENCE_SPEC,
  setSpec: () => {},
});

export function AssetProvider({ children }: { children: React.ReactNode }) {
  const [spec, setSpecState] = useState<AssetSpec>(REFERENCE_SPEC);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    const raw = localStorage.getItem("revopt:asset");
    if (raw) {
      try {
        setSpecState(JSON.parse(raw));
      } catch {}
    }
    setHydrated(true);
  }, []);

  function setSpec(s: AssetSpec) {
    setSpecState(s);
    localStorage.setItem("revopt:asset", JSON.stringify(s));
  }

  // Avoid hydration mismatch by rendering the default until client-side localStorage read finishes.
  if (!hydrated) {
    return (
      <AssetContext.Provider value={{ spec: REFERENCE_SPEC, setSpec }}>
        {children}
      </AssetContext.Provider>
    );
  }

  return (
    <AssetContext.Provider value={{ spec, setSpec }}>
      {children}
    </AssetContext.Provider>
  );
}

export function useAsset() {
  return useContext(AssetContext);
}

export const POWER_OPTIONS = [10, 25, 50, 100, 200] as const;
export const DURATION_OPTIONS: Array<1 | 2 | 4> = [1, 2, 4];
