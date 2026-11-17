import {
  CircleAlert,
  CircleCheck,
  Clock,
  Globe,
  Library,
  LoaderCircle,
  Minus,
  NotebookPen,
  TriangleAlert,
} from "lucide-react";
import type { ReactNode } from "react";
import type { Origin } from "@/lib/api/types";

export type Tone = "good" | "warn" | "crit" | "busy" | "waiting" | "neutral";

const TONE: Record<Tone, { className: string; icon: ReactNode }> = {
  good: { className: "bg-good-wash text-good-ink", icon: <CircleCheck className="size-3.5" aria-hidden /> },
  warn: { className: "bg-warn-wash text-warn-ink", icon: <TriangleAlert className="size-3.5" aria-hidden /> },
  crit: { className: "bg-crit-wash text-crit-ink", icon: <CircleAlert className="size-3.5" aria-hidden /> },
  busy: { className: "bg-sunken text-ink-2", icon: <LoaderCircle className="size-3.5 animate-spin" aria-hidden /> },
  waiting: { className: "bg-sunken text-ink-2", icon: <Clock className="size-3.5" aria-hidden /> },
  neutral: { className: "bg-sunken text-ink-2", icon: <Minus className="size-3.5" aria-hidden /> },
};

/** A state, always shown as an icon plus a word so it never depends on colour. */
export function Status({ tone, children }: { tone: Tone; children: ReactNode }) {
  const t = TONE[tone];
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[13px] font-semibold whitespace-nowrap ${t.className}`}
    >
      {t.icon}
      {children}
    </span>
  );
}

export const ORIGIN: Record<
  Origin,
  { label: string; long: string; highlight: string; mark: string; Icon: typeof Globe }
> = {
  class: {
    label: "Class",
    long: "From your class materials",
    highlight: "hl-class",
    mark: "bg-mark-class",
    Icon: NotebookPen,
  },
  external: {
    label: "External",
    long: "From an external source fetched for this course",
    highlight: "hl-external",
    mark: "bg-mark-external",
    Icon: Library,
  },
  web: {
    label: "Web",
    long: "From a live web search",
    highlight: "hl-web",
    mark: "bg-mark-web",
    Icon: Globe,
  },
};

/** Where something came from, in that origin's highlighter colour. */
export function OriginTag({ origin, className = "" }: { origin: Origin; className?: string }) {
  const o = ORIGIN[origin];
  return (
    <span
      title={o.long}
      className={`hl ${o.highlight} inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-[13px] font-semibold text-ink whitespace-nowrap ${className}`}
    >
      <o.Icon className="size-3.5" aria-hidden />
      {o.label}
    </span>
  );
}
