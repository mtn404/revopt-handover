/**
 * Inline SVG wordmark styled to match the Utilidex logo aesthetic
 * (lowercase, heavy weight, royal blue #2E6EE8).
 *
 * If/when you have the actual SVG asset, replace this component with
 * <Image src="/utilidex-logo.svg" ... /> in the Sidebar.
 */
export function UtilidexLogo({ className = "h-7 w-auto" }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 200 44"
      xmlns="http://www.w3.org/2000/svg"
      role="img"
      aria-label="utilidex"
    >
      <text
        x="0"
        y="34"
        fontFamily="ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
        fontSize="38"
        fontWeight="800"
        letterSpacing="-1.5"
        fill="#2E6EE8"
      >
        utilidex
      </text>
    </svg>
  );
}
