import { CircleAlert } from "lucide-react";
import type { ReactNode } from "react";
import { errorMessage } from "@/lib/format";
import { Button } from "./button";

/** Shown when a list has nothing in it yet. Says what to do next. */
export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-dashed border-rule-strong px-6 py-10 text-center">
      <p className="font-semibold text-ink">{title}</p>
      {children ? <p className="mx-auto mt-1 max-w-prose text-ink-2">{children}</p> : null}
      {action ? <div className="mt-4 flex justify-center">{action}</div> : null}
    </div>
  );
}

/** A failed load or action, with what went wrong and a way to try again. */
export function ErrorNote({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center gap-3 rounded-lg border border-crit/40 bg-crit-wash px-4 py-3 text-crit-ink"
    >
      <CircleAlert className="size-5 shrink-0" aria-hidden />
      <p className="min-w-0 flex-1">{errorMessage(error)}</p>
      {onRetry ? (
        <Button size="sm" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

/** Placeholder lines for a first load. Reloads keep the old content instead. */
export function LoadingLines({ lines = 4, label = "Loading" }: { lines?: number; label?: string }) {
  return (
    <div role="status" aria-label={label} className="space-y-3 py-2">
      {Array.from({ length: lines }, (_, i) => (
        <div
          key={i}
          className="h-4 animate-pulse rounded bg-sunken"
          style={{ width: `${92 - ((i * 17) % 40)}%` }}
        />
      ))}
    </div>
  );
}
