"use client";

import Link from "next/link";
import Image from "next/image";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  LineChart,
  BatteryCharging,
  Gavel,
  Activity,
  Settings,
} from "lucide-react";
import { cn } from "@/lib/utils";

const UTILIDEX_LOGO_WHITE =
  "https://cdn.prod.website-files.com/66f126e9f96aa663830b5cef/6708ded1cafcb6bb281c652b_24px%20H%20-%20utilidex%20white.svg";

const NAV = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/forecasts", label: "Forecasts", icon: LineChart },
  { href: "/dispatch", label: "Dispatch", icon: BatteryCharging },
  { href: "/ancillary", label: "Ancillary bids", icon: Gavel },
  { href: "/revenue", label: "Revenue", icon: Activity },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="w-60 shrink-0 flex flex-col">
      {/* Brand header — connects seamlessly to the topbar (no right border) */}
      <div className="h-16 flex items-center pl-6 bg-brand">
        <Image
          src={UTILIDEX_LOGO_WHITE}
          alt="Utilidex"
          width={140}
          height={32}
          priority
          unoptimized
          className="h-8 w-auto"
        />
      </div>

      {/* Nav + footer — white surface with right border */}
      <div className="flex-1 flex flex-col bg-bg-surface border-r border-bg-border">
        <nav className="flex-1 px-3 py-4 space-y-0.5">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = pathname === href;
            return (
              <Link
                key={href}
                href={href}
                className={cn(
                  "flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors",
                  active
                    ? "bg-brand-light text-brand-dark font-medium"
                    : "text-fg-muted hover:bg-bg-elevated hover:text-fg"
                )}
              >
                <Icon className="h-4 w-4" strokeWidth={2} />
                <span>{label}</span>
              </Link>
            );
          })}
        </nav>

        <div className="px-3 py-4 border-t border-bg-border">
          <div className="flex items-center gap-3 px-2">
            <div className="h-8 w-8 rounded-full bg-bg-elevated flex items-center justify-center text-xs font-semibold text-fg-muted">
              UD
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-sm text-fg truncate">Operator</div>
              <div className="text-[11px] text-fg-muted truncate">Utilidex demo</div>
            </div>
          </div>
        </div>
      </div>
    </aside>
  );
}
