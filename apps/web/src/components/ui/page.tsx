import type { ReactNode } from "react";

/** The title block at the top of every screen. */
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-8 flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
      <div className="min-w-0">
        <h1 className="text-[28px] leading-tight font-bold tracking-[-0.01em] text-ink">{title}</h1>
        {description ? <p className="mt-1.5 max-w-[68ch] text-ink-2">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

/** A titled block of a screen, on a white sheet. */
export function Panel({
  title,
  description,
  actions,
  children,
  className = "",
  flush = false,
}: {
  title?: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  /** Remove the inner padding, for tables that run edge to edge. */
  flush?: boolean;
}) {
  return (
    <section className={`rounded-lg border border-rule bg-surface ${className}`}>
      {title ? (
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 px-5 pt-4 pb-3">
          <div className="min-w-0">
            <h2 className="text-[17px] leading-snug font-bold text-ink">{title}</h2>
            {description ? <p className="mt-0.5 text-sm text-ink-2">{description}</p> : null}
          </div>
          {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
        </div>
      ) : null}
      <div className={flush ? "" : `px-5 pb-5 ${title ? "" : "pt-5"}`}>{children}</div>
    </section>
  );
}

/** A single headline number with its label. */
export function StatTile({
  label,
  value,
  note,
}: {
  label: string;
  value: string;
  note?: string;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-sm text-ink-2">{label}</dt>
      <dd className="mt-1 text-[26px] leading-none font-bold text-ink">{value}</dd>
      {note ? <dd className="mt-1.5 text-sm text-ink-3">{note}</dd> : null}
    </div>
  );
}

export function StatRow({ children }: { children: ReactNode }) {
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-5 rounded-lg border border-rule bg-surface px-5 py-4 lg:grid-cols-4">
      {children}
    </dl>
  );
}
