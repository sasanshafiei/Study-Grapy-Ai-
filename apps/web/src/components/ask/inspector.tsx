"use client";

import { ExternalLink, Quote } from "lucide-react";
import type { Citation } from "@/lib/api/types";
import { ORIGIN } from "@/components/ui/status";

/**
 * The citation inspector.
 *
 * Clicking a numbered marker in an answer opens the passage here, rather than
 * scrolling the reader away from the sentence they were reading. This is the
 * payoff of the whole product: every claim can be traced in one click.
 */

export function CitationDetail({ citation }: { citation: Citation }) {
  const origin = ORIGIN[citation.origin];
  return (
    <div>
      <div className="flex items-center gap-2">
        <span
          aria-hidden
          className={`flex size-6 shrink-0 items-center justify-center rounded-full text-[13px] font-bold text-ink ${origin.mark}`}
        >
          {citation.marker}
        </span>
        <span className="text-sm font-semibold text-ink">{origin.label}</span>
        <span className="text-sm text-ink-3">{citation.locator}</span>
      </div>

      <h3 className="mt-3 leading-snug font-bold break-words text-ink">{citation.title}</h3>

      <figure className="mt-3">
        <Quote className="size-4 text-ink-3" aria-hidden />
        <blockquote className={`reading mt-1 border-l-2 pl-3 text-ink ${origin.mark.replace("bg-", "border-")}`}>
          {citation.snippet}
        </blockquote>
      </figure>

      {citation.url ? (
        <a
          href={citation.url}
          target="_blank"
          rel="noreferrer noopener"
          className="mt-4 inline-flex items-center gap-1.5 text-sm font-semibold text-ink underline underline-offset-2"
        >
          Open the original
          <ExternalLink className="size-3.5" aria-hidden />
        </a>
      ) : (
        <p className="mt-4 text-sm text-ink-3">
          From a file uploaded to this course. Open it under Materials.
        </p>
      )}
    </div>
  );
}

export function Inspector({ citation }: { citation: Citation | null }) {
  return (
    <div className="h-full overflow-y-auto">
      <h2 className="mb-3 font-bold text-ink">Source</h2>
      {citation ? (
        <CitationDetail citation={citation} />
      ) : (
        <p className="rounded-lg border border-dashed border-rule-strong p-4 text-sm text-ink-3">
          Click a numbered marker in an answer, and the exact passage it came from appears here.
        </p>
      )}
    </div>
  );
}
