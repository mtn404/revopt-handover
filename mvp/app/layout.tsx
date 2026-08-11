import "./globals.css";
import type { Metadata } from "next";
import { Plus_Jakarta_Sans } from "next/font/google";
import { Sidebar } from "@/components/Sidebar";
import { Topbar } from "@/components/Topbar";
import { AssetProvider } from "@/lib/asset-context";

// Display font — refined modern geometric sans. Used by premium SaaS
// dashboards (Notion, Linear-adjacent). Clean, professional, with character.
const displayFont = Plus_Jakarta_Sans({
  subsets: ["latin"],
  weight: ["500", "600", "700", "800"],
  variable: "--font-display",
  display: "swap",
});

export const metadata: Metadata = {
  title: "RevOpt — BESS Optimisation Console",
  description:
    "Forecast-driven multi-market battery dispatch optimiser. Day-ahead, imbalance, and seven-product ancillary stack.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={displayFont.variable}>
      <body className="min-h-screen">
        <AssetProvider>
          <div className="flex h-screen">
            <Sidebar />
            <div className="flex-1 flex flex-col overflow-hidden">
              <Topbar />
              <main className="flex-1 overflow-y-auto bg-bg">
                <div className="mx-auto max-w-[1400px] px-6 py-6">{children}</div>
              </main>
            </div>
          </div>
        </AssetProvider>
      </body>
    </html>
  );
}
