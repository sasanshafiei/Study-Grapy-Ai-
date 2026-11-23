import Link from "next/link";
import { LoaderCircle } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";

type Variant = "primary" | "secondary" | "quiet" | "danger";
type Size = "md" | "sm";

const BASE =
  "inline-flex items-center justify-center gap-2 rounded-md font-semibold whitespace-nowrap transition-colors disabled:opacity-50";

const VARIANT: Record<Variant, string> = {
  primary: "bg-action text-action-ink hover:opacity-90",
  secondary: "border border-rule-strong bg-surface text-ink hover:bg-sunken",
  quiet: "text-ink-2 hover:bg-sunken hover:text-ink",
  danger: "border border-rule-strong bg-surface text-crit-ink hover:bg-crit-wash",
};

const SIZE: Record<Size, string> = {
  md: "h-10 px-4 text-[15px]",
  sm: "h-8 px-3 text-sm",
};

export function buttonClass(variant: Variant = "secondary", size: Size = "md", extra = "") {
  return `${BASE} ${VARIANT[variant]} ${SIZE[size]} ${extra}`;
}

interface ButtonProps extends ComponentProps<"button"> {
  variant?: Variant;
  size?: Size;
  /** Shows a spinner and blocks clicks while work is in progress. */
  busy?: boolean;
  icon?: ReactNode;
}

export function Button({
  variant = "secondary",
  size = "md",
  busy = false,
  icon,
  className = "",
  children,
  disabled,
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={buttonClass(variant, size, className)}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {busy ? <LoaderCircle className="size-4 animate-spin" aria-hidden /> : icon}
      {children}
    </button>
  );
}

interface ButtonLinkProps extends ComponentProps<typeof Link> {
  variant?: Variant;
  size?: Size;
  icon?: ReactNode;
}

export function ButtonLink({
  variant = "secondary",
  size = "md",
  icon,
  className = "",
  children,
  ...rest
}: ButtonLinkProps) {
  return (
    <Link className={buttonClass(variant, size, className)} {...rest}>
      {icon}
      {children}
    </Link>
  );
}
