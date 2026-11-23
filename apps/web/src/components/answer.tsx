"use client";

import { ExternalLink, TriangleAlert } from "lucide-react";
import type { Citation } from "@/lib/api/types";
import { ORIGIN, OriginTag } from "@/components/ui/status";

/**
 * How an answer is shown.
 *
 * Every sentence that came from a source is highlighted in that source's
 * colour and carries a numbered marker. The same colours repeat in the source
 * list below, so a reader can see at a glance how much of an answer rests on
 * class material and how much on something fetched from outside.
 */

interface Segment {
  text: string;
  marker: number | null;
}

/** Splits "A sentence. [1] Another. [2]" into runs tagged with their marker. */
export function splitByCitations(text: string): Segment[] {
  const segments: Segment[] = [];
  const pattern = /\s*\[(\d+)\]/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    segments.push({ text: text.slice(cursor, match.index), marker: Number(match[1]) });
    cursor = match.index + match[0].length;
  }
  const tail = text.slice(cursor).trim();
  if (tail) segments.push({ text: tail, marker: null });
  return segments;
}

export function AnswerText({
  text,
  citations,
  onCitationClick,
}: {
  text: string;
  citations: Citation[];
  onCitationClick?: (marker: number) => void;
}) {
  const byMarker = new Map(citations.map((c) => [c.marker, c]));

  return (
    <p className="reading text-ink">
      {splitByCitations(text).map((segment, i) => {
        const citation = segment.marker === null ? null : byMarker.get(segment.marker);
        const body = segment.text.trim();
        if (!citation) return <span key={i}>{body} </span>;
        return (
          <span key={i}>
            <mark className={`hl ${ORIGIN[citation.origin].highlight}`}>{body}</mark>
            <button
              type="button"
              onClick={() => onCitationClick?.(citation.marker)}
              title={`Source ${citation.marker}: ${citation.title}`}
              className="ml-0.5 align-super text-[0.7em] font-bold text-ink-2 underline decoration-dotted underline-offset-2 hover:text-ink"
            >
              {citation.marker}
            </button>{" "}
          </span>
        );
      })}
    </p>
  );
}

/** One source behind an answer: where it is, and what it said. */
export function SourceCard({ citation, id }: { citation: Citation; id?: string }) {
  const origin = ORIGIN[citation.origin];
  return (
    <li
      id={id}
      className="flex gap-3 rounded-md border border-rule bg-surface p-3 target:ring-2 target:ring-focus"
    >
      <span
        aria-hidden
        className={`mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full text-[13px] font-bold text-ink ${origin.mark}`}
      >
        {citation.marker}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <OriginTag origin={citation.origin} />
          <span className="text-sm text-ink-3">{citation.locator}</span>
        </div>
        <p className="mt-1 font-semibold break-words text-ink">{citation.title}</p>
        <p className="mt-1 text-sm text-ink-2">{citation.snippet}</p>
        {citation.url ? (
          <a
            href={citation.url}
            target="_blank"
            rel="noreferrer noopener"
            className="mt-1.5 inline-flex items-center gap-1 text-sm font-semibold text-ink underline underline-offset-2"
          >
            Open the source
            <ExternalLink className="size-3.5" aria-hidden />
          </a>
        ) : null}
      </div>
    </li>
  );
}

/** Shown when an answer has nothing to stand on. */
export function NotGroundedNote({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex gap-3 rounded-md border border-warn/40 bg-warn-wash px-4 py-3 text-warn-ink">
      <TriangleAlert className="mt-0.5 size-5 shrink-0" aria-hidden />
      <p className="min-w-0">{children}</p>
    </div>
  );
}

/** The colour key, so the highlighting is readable without guessing. */
export function OriginKey({ className = "" }: { className?: string }) {
  return (
    <div className={`flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-ink-2 ${className}`}>
      <span className="font-semibold text-ink">Highlight colours</span>
      {(["class", "external", "web"] as const).map((origin) => (
        <span key={origin} className="flex items-center gap-1.5">
          <span aria-hidden className={`size-3 rounded-sm ${ORIGIN[origin].mark}`} />
          {ORIGIN[origin].long}
        </span>
      ))}
    </div>
  );
}
