import type { ComponentProps, ReactNode } from "react";

const CONTROL =
  "w-full rounded-md border border-rule-strong bg-surface px-3 text-[15px] text-ink placeholder:text-ink-3 disabled:opacity-60";

interface FieldProps {
  label: string;
  htmlFor: string;
  hint?: string;
  error?: string | null;
  children: ReactNode;
  className?: string;
}

/** A labelled form control with optional help text and an inline error. */
export function Field({ label, htmlFor, hint, error, children, className = "" }: FieldProps) {
  return (
    <div className={className}>
      <label htmlFor={htmlFor} className="mb-1.5 block text-sm font-semibold text-ink">
        {label}
      </label>
      {children}
      {hint && !error ? <p className="mt-1.5 text-sm text-ink-3">{hint}</p> : null}
      {error ? (
        <p className="mt-1.5 text-sm text-crit-ink" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function Input({ className = "", ...rest }: ComponentProps<"input">) {
  return <input className={`${CONTROL} h-10 ${className}`} {...rest} />;
}

export function Select({ className = "", children, ...rest }: ComponentProps<"select">) {
  return (
    <select className={`${CONTROL} h-10 pr-8 ${className}`} {...rest}>
      {children}
    </select>
  );
}

export function Textarea({ className = "", ...rest }: ComponentProps<"textarea">) {
  return <textarea className={`${CONTROL} py-2 leading-relaxed ${className}`} {...rest} />;
}
