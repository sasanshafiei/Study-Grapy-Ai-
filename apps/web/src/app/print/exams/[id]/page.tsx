"use client";

import { Printer } from "lucide-react";
import { useParams } from "next/navigation";
import { Suspense, useState } from "react";
import { api } from "@/lib/api";
import { fullDate, plural } from "@/lib/format";
import { useQuery } from "@/lib/use-query";
import { Button } from "@/components/ui/button";
import { ErrorNote, LoadingLines } from "@/components/ui/feedback";

const LETTERS = ["A", "B", "C", "D", "E", "F"];

/**
 * A printable exam paper, and its answer key.
 *
 * Deliberately plain: black on white, no app chrome, so it photocopies well.
 * The toolbar is hidden when printing.
 */
function PrintExam() {
  const { id } = useParams<{ id: string }>();
  const { data, error, reload } = useQuery(`exam:${id}`, () => api.getExam(id));
  const [withKey, setWithKey] = useState(false);

  if (error) {
    return (
      <div className="mx-auto max-w-3xl p-8">
        <ErrorNote error={error} onRetry={reload} />
      </div>
    );
  }
  if (!data) {
    return (
      <div className="mx-auto max-w-3xl p-8">
        <LoadingLines label="Loading exam" />
      </div>
    );
  }

  const items = data.items.filter((i) => i.status !== "rejected");

  return (
    <div className="mx-auto min-h-dvh max-w-[46rem] bg-surface px-8 py-10 text-ink print:max-w-none print:px-0 print:py-0">
      <div className="no-print mb-8 flex flex-wrap items-center gap-3 rounded-lg border border-rule bg-sunken px-4 py-3">
        <Button variant="primary" icon={<Printer className="size-4" aria-hidden />} onClick={() => window.print()}>
          Print
        </Button>
        <label className="flex items-center gap-2 text-ink">
          <input
            type="checkbox"
            checked={withKey}
            onChange={(e) => setWithKey(e.target.checked)}
            className="size-4 accent-[var(--series)]"
          />
          Include the answer key
        </label>
        {data.status !== "approved" ? (
          <p className="text-sm text-warn-ink">
            This exam is not approved yet. Review every question before handing it out.
          </p>
        ) : null}
      </div>

      <header className="border-b-2 border-ink pb-4">
        <h1 className="text-2xl font-bold">{data.title}</h1>
        <p className="mt-1 text-ink-2">
          {plural(items.length, "question")} · {data.totalPoints} points · {fullDate(data.createdAt)}
        </p>
        <div className="mt-5 grid grid-cols-2 gap-6 text-sm">
          <p className="border-b border-ink-3 pb-1">Name</p>
          <p className="border-b border-ink-3 pb-1">Student number</p>
        </div>
      </header>

      <ol className="mt-8 space-y-7">
        {items.map((item, index) => (
          <li key={item.id} className="break-inside-avoid">
            <div className="flex items-baseline gap-3">
              <span className="font-bold">{index + 1}.</span>
              <p className="reading flex-1">{item.stem}</p>
              <span className="text-sm whitespace-nowrap text-ink-2">
                [{plural(item.points, "pt", "pts")}]
              </span>
            </div>
            {item.choices.length > 0 ? (
              <ol className="mt-2 ml-9 space-y-1.5">
                {item.choices.map((choice, c) => (
                  <li key={choice} className="flex gap-2">
                    <span className="font-semibold">{LETTERS[c]}.</span>
                    <span>{choice}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="mt-3 ml-9 space-y-5" aria-hidden>
                {[0, 1, 2].map((line) => (
                  <div key={line} className="border-b border-ink-3" />
                ))}
              </div>
            )}
          </li>
        ))}
      </ol>

      {withKey ? (
        <section className="mt-12 break-before-page border-t-2 border-ink pt-6">
          <h2 className="text-xl font-bold">Answer key: {data.title}</h2>
          <ol className="mt-4 space-y-4">
            {items.map((item, index) => (
              <li key={item.id} className="break-inside-avoid">
                <p>
                  <span className="font-bold">{index + 1}.</span>{" "}
                  <span className="font-semibold">
                    {item.choices.length > 0
                      ? `${LETTERS[item.choices.indexOf(item.answer)] ?? "?"}. ${item.answer}`
                      : item.answer}
                  </span>
                </p>
                {item.rubric ? (
                  <p className="mt-1 ml-5 text-sm text-ink-2">{item.rubric}</p>
                ) : null}
                <p className="mt-1 ml-5 text-sm text-ink-3">
                  Source: {item.citation.title}, {item.citation.locator}
                </p>
              </li>
            ))}
          </ol>
        </section>
      ) : null}
    </div>
  );
}

export default function PrintExamPage() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-3xl p-8">
          <LoadingLines label="Loading exam" />
        </div>
      }
    >
      <PrintExam />
    </Suspense>
  );
}
