import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Light theme, Utilidex sea blue
        bg: {
          DEFAULT: "#F4F7FA",        // page background — light cool gray
          surface: "#FFFFFF",        // card / panel
          elevated: "#EDF1F5",       // hover / active
          border: "#E1E6ED",         // soft divider
          tint: "#F0F8FC",           // brand-tinted subtle highlight
        },
        fg: {
          DEFAULT: "#1A2330",        // primary text — dark navy
          muted: "#5A6B7D",          // secondary text
          subtle: "#8A95A3",         // tertiary / disabled
        },
        brand: {
          DEFAULT: "#2E6EE8",        // Utilidex blue (matches logo)
          dark: "#1F58C9",           // hover / active
          mid: "#5388EE",            // mid tint
          light: "#E3EDFC",          // very pale wash
        },
        accent: {
          teal: "#14B8A6",
          gold: "#E8A33D",
          amber: "#F0AB36",          // for charge (counterpart to sea-blue discharge)
          red: "#E54848",
          green: "#16A34A",          // for positive deltas
          purple: "#7C3AED",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        // Display font — used for branded headings (Utilidex wordmark feel)
        display: [
          "var(--font-display)",
          "Poppins",
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "sans-serif",
        ],
        mono: ["JetBrains Mono", "ui-monospace", "Menlo", "Monaco", "Consolas", "monospace"],
      },
      boxShadow: {
        card: "0 1px 2px rgba(15, 30, 55, 0.04), 0 1px 3px rgba(15, 30, 55, 0.06)",
        elevated: "0 4px 12px rgba(15, 30, 55, 0.08), 0 2px 4px rgba(15, 30, 55, 0.04)",
      },
      borderRadius: {
        DEFAULT: "0.5rem",
        lg: "0.75rem",
        xl: "1rem",
      },
    },
  },
  plugins: [],
};

export default config;
