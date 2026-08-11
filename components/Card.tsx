import { cn } from "@/lib/utils";

export function Card({
  children,
  className,
  title,
  subtitle,
  action,
}: {
  children: React.ReactNode;
  className?: string;
  title?: string;
  subtitle?: string;
  action?: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "bg-bg-surface border border-bg-border rounded-lg shadow-card",
        className
      )}
    >
      {(title || action) && (
        <div className="flex items-start justify-between px-5 pt-4 pb-2 border-b border-bg-border/60">
          <div>
            {title && (
              <h3 className="text-[13px] font-semibold text-fg tracking-tight">{title}</h3>
            )}
            {subtitle && (
              <p className="text-[11px] text-fg-muted mt-0.5">{subtitle}</p>
            )}
          </div>
          {action}
        </div>
      )}
      <div className={cn("px-5", title ? "py-5" : "py-5")}>{children}</div>
    </div>
  );
}

export function Stat({
  label,
  value,
  delta,
  deltaPositive,
  hint,
}: {
  label: string;
  value: string;
  delta?: string;
  deltaPositive?: boolean;
  hint?: string;
}) {
  return (
    <div>
      <div className="text-[11px] uppercase text-fg-muted tracking-wider font-medium">
        {label}
      </div>
      <div className="mt-1.5 flex items-baseline gap-2 tabular">
        <div className="text-[28px] font-semibold text-fg tracking-tight leading-none">
          {value}
        </div>
        {delta && (
          <div
            className={cn(
              "text-xs font-medium",
              deltaPositive ? "text-accent-green" : "text-accent-red"
            )}
          >
            {delta}
          </div>
        )}
      </div>
      {hint && <div className="text-[11px] text-fg-muted mt-1.5">{hint}</div>}
    </div>
  );
}

export function Badge({
  children,
  variant = "default",
}: {
  children: React.ReactNode;
  variant?: "default" | "brand" | "gold" | "red" | "green";
}) {
  const styles: Record<string, string> = {
    default: "bg-bg-elevated text-fg-muted border border-bg-border",
    brand:   "bg-brand-light text-brand-dark border border-brand/20",
    gold:    "bg-accent-gold/12 text-accent-gold border border-accent-gold/25",
    red:     "bg-accent-red/10 text-accent-red border border-accent-red/25",
    green:   "bg-accent-green/10 text-accent-green border border-accent-green/25",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center px-2 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider",
        styles[variant]
      )}
    >
      {children}
    </span>
  );
}
