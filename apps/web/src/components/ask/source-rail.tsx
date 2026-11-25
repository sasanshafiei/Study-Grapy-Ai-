"use client";

import { Globe, Layers } from "lucide-react";
import type { DocumentItem } from "@/lib/api/types";
import { ORIGIN } from "@/components/ui/status";

/**
 * The source picker.
 *
 * Which documents an answer may draw on is the single most important control
 * on this screen, so it sits beside the conversation rather than behind a
 * menu. Ticking a source changes what the next answer can cite; the web
 * toggle is separate because live results are not part of the course index.
 */

function Group({
  label,
  documents,
  selected,
  onToggle,
}: {
  label: string;
  documents: DocumentItem[];
  selected: Set<string>;
  onToggle: (id: string) => void;
}) {
  if (documents.length === 0) return null;
  return (
    <section className="mb-4">
      <h3 className="mb-1.5 text-[13px] font-semibold text-ink-3">{label}</h3>
      <ul className="space-y-0.5">
        {documents.map((doc) => {
          const origin = ORIGIN[doc.origin];
          const on = selected.has(doc.id);
          return (
            <li key={doc.id}>
              <label
                className={`flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 text-sm transition-colors ${
                  on ? "text-ink" : "text-ink-3"
                } hover:bg-sunken`}
              >
                <input
                  type="checkbox"
                  checked={on}
                  onChange={() => onToggle(doc.id)}
                  className="mt-0.5 size-3.5 shrink-0 accent-[var(--series)]"
                />
                <span
                  aria-hidden
                  className={`mt-1 size-2 shrink-0 rounded-sm ${origin.mark} ${on ? "" : "opacity-40"}`}
                />
                <span className="min-w-0 flex-1 leading-snug break-words">{doc.title}</span>
              </label>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function SourceRail({
  documents,
  selected,
  onChange,
  useWeb,
  onUseWebChange,
}: {
  documents: DocumentItem[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
  useWeb: boolean;
  onUseWebChange: (next: boolean) => void;
}) {
  const usable = documents.filter((d) => d.status === "ready");
  const classDocs = usable.filter((d) => d.origin === "class");
  const fetched = usable.filter((d) => d.origin !== "class");

  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(next);
  };

  const setAll = (docs: DocumentItem[]) => onChange(new Set(docs.map((d) => d.id)));

  const quiet =
    "rounded px-1.5 py-0.5 text-[13px] font-semibold text-ink-2 hover:bg-sunken hover:text-ink";

  return (
    <div className="flex h-full flex-col">
      <div className="mb-3 flex items-baseline justify-between gap-2">
        <h2 className="flex items-center gap-1.5 font-bold text-ink">
          <Layers className="size-4" aria-hidden />
          Sources
        </h2>
        <span className="text-[13px] text-ink-3 tabular-nums">
          {selected.size} of {usable.length}
        </span>
      </div>

      <div className="mb-3 flex flex-wrap gap-1">
        <button type="button" className={quiet} onClick={() => setAll(usable)}>
          All
        </button>
        <button type="button" className={quiet} onClick={() => setAll(classDocs)}>
          Class only
        </button>
        <button type="button" className={quiet} onClick={() => onChange(new Set())}>
          None
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto pr-1">
        <Group label="Your class" documents={classDocs} selected={selected} onToggle={toggle} />
        <Group label="Fetched for this course" documents={fetched} selected={selected} onToggle={toggle} />
        {usable.length === 0 ? (
          <p className="text-sm text-ink-3">
            Nothing is indexed yet. Once materials are ready they appear here.
          </p>
        ) : null}
      </div>

      <label className="mt-3 flex cursor-pointer items-start gap-2 border-t border-rule pt-3 text-sm text-ink">
        <input
          type="checkbox"
          checked={useWeb}
          onChange={(e) => onUseWebChange(e.target.checked)}
          className="mt-0.5 size-3.5 shrink-0 accent-[var(--series)]"
        />
        <span className="min-w-0">
          <span className="flex items-center gap-1.5 font-semibold">
            <Globe className="size-3.5" aria-hidden />
            Search the web too
          </span>
          <span className="mt-0.5 block text-[13px] text-ink-3">
            Slower, and not reviewed by your professor.
          </span>
        </span>
      </label>
    </div>
  );
}

/** One line summarising the selection, for the narrow layout. */
export function SourceSummary({
  documents,
  selected,
  useWeb,
}: {
  documents: DocumentItem[];
  selected: Set<string>;
  useWeb: boolean;
}) {
  const usable = documents.filter((d) => d.status === "ready");
  return (
    <>
      {selected.size} of {usable.length} sources
      {useWeb ? " · web search on" : ""}
    </>
  );
}
