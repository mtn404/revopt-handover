/**
 * DUoS (Distribution Use of System) tariff lookup + computation.
 *
 * Each GB distribution licence area is operated by one of 6 DNOs and publishes
 * a CDCM (Common Distribution Charging Methodology) tariff each tariff year
 * (April → March). The HH (half-hourly) unit rate depends on:
 *   • Region (one of 14 licence areas, here grouped into 8 representative)
 *   • Voltage level (LV / HV / EHV)
 *   • Time-of-use band (Red / Amber / Green)
 *
 * Unit rates below are TYPICAL of published CDCM 2025/26 schedules — they are
 * representative for analytical purposes. For a real bid you would replace
 * these with the exact published rates for your specific DNO and HH metering
 * class. The structure is the same.
 *
 * Sources to refresh annually:
 *   • UKPN  https://www.ukpowernetworks.co.uk/internet/en/about-us/charges/
 *   • WPD   https://www.nationalgrid.co.uk/connections-and-charges/charges
 *   • NPg   https://www.northernpowergrid.com/charging
 *   • SPEN  https://www.spenergynetworks.co.uk/pages/charging.aspx
 *   • SSEN  https://ssen-distribution.co.uk/library/charging-statements/
 *   • ENW   https://www.enwl.co.uk/zero-carbon/our-network/charging-statements/
 */

export type DuosVoltage = "lv" | "hv" | "ehv";
export type DuosBand    = "red" | "amber" | "green";

export type DuosTariff = {
  /** £/kWh consumed (or exported in some tariffs); applied per band per SP */
  rates: Record<DuosVoltage, Record<DuosBand, number>>;
};

export const DUOS_REGIONS: Record<string, { name: string; tariff: DuosTariff }> = {
  lpn: {
    name: "London (UKPN LPN)",
    tariff: {
      rates: {
        lv:  { red: 0.0876, amber: 0.0246, green: 0.0014 },
        hv:  { red: 0.0512, amber: 0.0152, green: 0.0008 },
        ehv: { red: 0.0214, amber: 0.0048, green: 0.0001 },
      },
    },
  },
  spn: {
    name: "South East (UKPN SPN)",
    tariff: {
      rates: {
        lv:  { red: 0.0712, amber: 0.0208, green: 0.0012 },
        hv:  { red: 0.0421, amber: 0.0128, green: 0.0007 },
        ehv: { red: 0.0186, amber: 0.0041, green: 0.0001 },
      },
    },
  },
  epn: {
    name: "East (UKPN EPN)",
    tariff: {
      rates: {
        lv:  { red: 0.0698, amber: 0.0204, green: 0.0011 },
        hv:  { red: 0.0412, amber: 0.0124, green: 0.0007 },
        ehv: { red: 0.0181, amber: 0.0040, green: 0.0001 },
      },
    },
  },
  wpd_smid: {
    name: "South Midlands (NG WPD)",
    tariff: {
      rates: {
        lv:  { red: 0.0658, amber: 0.0189, green: 0.0010 },
        hv:  { red: 0.0387, amber: 0.0114, green: 0.0006 },
        ehv: { red: 0.0168, amber: 0.0036, green: 0.0001 },
      },
    },
  },
  wpd_swales: {
    name: "South Wales (NG WPD)",
    tariff: {
      rates: {
        lv:  { red: 0.0734, amber: 0.0212, green: 0.0012 },
        hv:  { red: 0.0432, amber: 0.0130, green: 0.0007 },
        ehv: { red: 0.0192, amber: 0.0044, green: 0.0001 },
      },
    },
  },
  npg_yorkshire: {
    name: "Yorkshire (Northern Powergrid)",
    tariff: {
      rates: {
        lv:  { red: 0.0581, amber: 0.0168, green: 0.0009 },
        hv:  { red: 0.0341, amber: 0.0102, green: 0.0005 },
        ehv: { red: 0.0148, amber: 0.0034, green: 0.0001 },
      },
    },
  },
  spen_spd: {
    name: "South Scotland (SP Energy Networks)",
    tariff: {
      rates: {
        lv:  { red: 0.0612, amber: 0.0176, green: 0.0010 },
        hv:  { red: 0.0358, amber: 0.0108, green: 0.0006 },
        ehv: { red: 0.0156, amber: 0.0036, green: 0.0001 },
      },
    },
  },
  ssen_sepd: {
    name: "Southern (SSEN SEPD)",
    tariff: {
      rates: {
        lv:  { red: 0.0689, amber: 0.0198, green: 0.0011 },
        hv:  { red: 0.0405, amber: 0.0121, green: 0.0006 },
        ehv: { red: 0.0178, amber: 0.0039, green: 0.0001 },
      },
    },
  },
};

export type DuosRegionId = keyof typeof DUOS_REGIONS;

/**
 * Map a half-hour settlement period (1-48) + date to a Red/Amber/Green band.
 * Generic pattern used across DNOs (regional offsets exist in real tariffs
 * but are within ~1 hour; this is a good first-order approximation).
 *
 *   Winter (Nov–Feb):
 *     Red:    16:00 – 19:00 weekdays
 *     Amber:  07:00 – 16:00 & 19:00 – 23:00 weekdays
 *     Green:  all weekends + 23:00 – 07:00 weekdays
 *   Summer (Mar–Oct):
 *     Amber:  07:00 – 23:00 weekdays
 *     Green:  rest
 */
export function periodToBand(period: number, date: Date): DuosBand {
  const hour = Math.floor((period - 1) / 2);             // 0..23
  const dow  = date.getUTCDay();                         // 0=Sun, 6=Sat
  const isWeekend = dow === 0 || dow === 6;
  const month = date.getUTCMonth();                      // 0=Jan, 11=Dec
  const isWinter = month >= 10 || month <= 1;            // Nov, Dec, Jan, Feb

  if (isWeekend) return "green";
  if (isWinter) {
    if (hour >= 16 && hour < 19) return "red";
    if ((hour >= 7 && hour < 16) || (hour >= 19 && hour < 23)) return "amber";
    return "green";
  }
  // Summer
  if (hour >= 7 && hour < 23) return "amber";
  return "green";
}

/**
 * Compute DUoS charge for one day's dispatch.
 *
 * Sign convention: DUoS unit rates are charged on energy IMPORTED from the
 * grid (charging the battery). Exported energy (discharge to grid) typically
 * does NOT pay DUoS unit charge (and may even credit a TNUoS trade unit, but
 * we ignore that here). So we apply rates only to the charge side.
 *
 * Returns the total charge in £ for the day.
 */
export function computeDuosDayCharge(
  dispatch: Array<{ period: number; pc_mw: number; pd_mw?: number }>,
  date: Date,
  region: DuosRegionId,
  voltage: DuosVoltage,
): number {
  const tariff = DUOS_REGIONS[region]?.tariff;
  if (!tariff) return 0;
  const rates = tariff.rates[voltage];
  let totalCharge = 0;
  for (const sp of dispatch) {
    const band = periodToBand(sp.period, date);
    const importMWh = Math.max(0, sp.pc_mw) * 0.5;       // charging = importing
    const importKWh = importMWh * 1000;
    totalCharge += importKWh * rates[band];
  }
  return totalCharge;
}

export const DUOS_VOLTAGE_LABELS: Record<DuosVoltage, string> = {
  lv:  "LV (≤ 1 kV)",
  hv:  "HV (1 kV – 22 kV)",
  ehv: "EHV (≥ 33 kV)",
};
