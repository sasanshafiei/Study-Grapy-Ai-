"use client";

import { Check, LoaderCircle, Pencil, Printer, RefreshCw, X } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Suspense, useState } from "react";
import { api, type Exam, type ExamFlag, type ExamItem } from "@/lib/api";
import { errorMessage, plural } from "@/lib/format";
import { usePolling, useQuery } from "@/lib/use-query";
import { SourceCard } from "@/components/answer";
import { Button, buttonClass } from "@/components/ui/button";
import { Field, Input, Textarea } from "@/components/ui/field";
import { ErrorNote, LoadingLines } from "@/components/ui/feedback";
import { PageHeader, Panel } from "@/components/ui/page";
import { Status } from "@/components/ui/status";
import { useToast } from "@/components/ui/toast";

const FLAG_TEXT: Record<ExamFlag, string> = {
  possible_duplicate: "Looks close to another question in this exam",
  weak_source: "The supporting passage is thin, so check it carefully",
  ambiguous: "More than one answer could be defended",
};

const TYPE_LABEL = {
  mcq: "Multiple choice",
  true_false: "True or false",
  short: "Short answer",
} as const;

function ItemCard({
  item,
  position,
  onPatch,
  onRegenerate,
}: {
  item: ExamItem;
  position: number;
  onPatch: (patch: Parameters<typeof api.updateExamItem>[2]) => Promise<void>;
  onRegenerate: () => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [stem, setStem] = useState(item.stem);
  const [answer, setAnswer] = useState(item.answer);
  const [rubric, setRubric] = useState(item.rubric);
  const [points, setPoints] = useState(item.points);
  const [busy, setBusy] = useState<"save" | "approve" | "reject" | "regenerate" | null>(null);

  async function run(kind: NonNullable<typeof busy>, work: () => Promise<void>) {
    setBusy(kind);
    try {
      await work();
    } finally {
      setBusy(null);
    }
  }

  const border =
    item.status === "approved"
      ? "border-good/50"
      : item.status === "rejected"
        ? "border-rule opacity-60"
        : "border-rule";

  return (
    <li className={`rounded-lg border bg-surface p-5 ${border}`}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-ink-3">
          <span className="font-bold text-ink">Q{position}</span>
          <span>{item.topicName}</span>
          <span aria-hidden>·</span>
          <span>{TYPE_LABEL[item.type]}</span>
          <span aria-hidden>·</span>
          <span>{item.difficulty}</span>
          <span aria-hidden>·</span>
          <span>{plural(item.points, "point")}</span>
        </div>
        {item.status === "approved" ? (
          <Status tone="good">Approved</Status>
        ) : item.status === "rejected" ? (
          <Status tone="neutral">Rejected</Status>
        ) : (
          <Status tone="waiting">Needs review</Status>
        )}
      </div>

      {item.flags.length > 0 ? (
        <ul className="mt-3 space-y-1">
          {item.flags.map((flag) => (
            <li key={flag}>
              <Status tone="warn">{FLAG_TEXT[flag]}</Status>
            </li>
          ))}
        </ul>
      ) : null}

      {editing ? (
        <div className="mt-4 space-y-3">
          <Field label="Question" htmlFor={`stem-${item.id}`}>
            <Textarea
              id={`stem-${item.id}`}
              rows={3}
              value={stem}
              onChange={(e) => setStem(e.target.value)}
            />
          </Field>
          <Field
            label={item.type === "short" ? "Model answer" : "Correct answer"}
            htmlFor={`answer-${item.id}`}
            hint={
              item.type === "mcq"
                ? "Must match one of the choices exactly."
                : undefined
            }
          >
            <Textarea
              id={`answer-${item.id}`}
              rows={item.type === "short" ? 3 : 1}
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
            />
          </Field>
          {item.type === "short" ? (
            <Field label="Marking guidance" htmlFor={`rubric-${item.id}`}>
              <Textarea
                id={`rubric-${item.id}`}
                rows={3}
                value={rubric}
                onChange={(e) => setRubric(e.target.value)}
              />
            </Field>
          ) : null}
          <Field label="Points" htmlFor={`points-${item.id}`} className="max-w-32">
            <Input
              id={`points-${item.id}`}
              type="number"
              min={1}
              max={50}
              value={points}
              onChange={(e) => setPoints(Math.max(1, Math.min(50, Number(e.target.value) || 1)))}
            />
          </Field>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="primary"
              busy={busy === "save"}
              onClick={() =>
                void run("save", async () => {
                  await onPatch({ stem, answer, rubric, points });
                  setEditing(false);
                })
              }
            >
              Save changes
            </Button>
            <Button
              onClick={() => {
                setStem(item.stem);
                setAnswer(item.answer);
                setRubric(item.rubric);
                setPoints(item.points);
                setEditing(false);
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <>
          <p className="reading mt-3 font-semibold text-ink">{item.stem}</p>

          {item.choices.length > 0 ? (
            <ul className="mt-3 space-y-1.5">
              {item.choices.map((choice) => {
                const correct = choice === item.answer;
                return (
                  <li
                    key={choice}
                    className={`flex items-start gap-2 rounded-md px-3 py-2 ${
                      correct ? "bg-good-wash font-semibold text-good-ink" : "text-ink-2"
                    }`}
                  >
                    {correct ? (
                      <Check className="mt-0.5 size-4 shrink-0" aria-hidden />
                    ) : (
                      <span className="mt-0.5 size-4 shrink-0" />
                    )}
                    <span className="min-w-0">
                      {choice}
                      {correct ? <span className="sr-only"> (correct answer)</span> : null}
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <div className="mt-3 rounded-md bg-sunken px-3 py-2">
              <p className="text-sm font-semibold text-ink">Model answer</p>
              <p className="reading mt-1 text-ink-2">{item.answer}</p>
              {item.rubric ? (
                <>
                  <p className="mt-2 text-sm font-semibold text-ink">Marking guidance</p>
                  <p className="mt-1 text-sm text-ink-2">{item.rubric}</p>
                </>
              ) : null}
            </div>
          )}

          <ul className="mt-4">
            <SourceCard citation={item.citation} />
          </ul>

          <div className="mt-4 flex flex-wrap gap-2">
            {item.status !== "approved" ? (
              <Button
                variant="primary"
                size="sm"
                busy={busy === "approve"}
                icon={<Check className="size-4" aria-hidden />}
                onClick={() => void run("approve", () => onPatch({ status: "approved" }))}
              >
                Approve
              </Button>
            ) : null}
            {item.status !== "rejected" ? (
              <Button
                size="sm"
                busy={busy === "reject"}
                icon={<X className="size-4" aria-hidden />}
                onClick={() => void run("reject", () => onPatch({ status: "rejected" }))}
              >
                Reject
              </Button>
            ) : null}
            <Button
              size="sm"
              icon={<Pencil className="size-4" aria-hidden />}
              onClick={() => setEditing(true)}
            >
              Edit
            </Button>
            <Button
              size="sm"
              busy={busy === "regenerate"}
              icon={<RefreshCw className="size-4" aria-hidden />}
              onClick={() => void run("regenerate", onRegenerate)}
            >
              Write another
            </Button>
          </div>
        </>
      )}
    </li>
  );
}

function ExamReview() {
  const params = useParams<{ id: string }>();
  const examId = params.id;
  const toast = useToast();
  const { data, error, reload, setData } = useQuery(`exam:${examId}`, () => api.getExam(examId));
  const [approving, setApproving] = useState(false);

  usePolling(data?.status === "generating", reload, 1500);

  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data) return <LoadingLines lines={6} label="Loading exam" />;

  const exam: Exam = data;
  const pending = exam.items.filter((i) => i.status === "pending").length;
  const kept = exam.items.filter((i) => i.status !== "rejected");

  async function patchItem(itemId: string, patch: Parameters<typeof api.updateExamItem>[2]) {
    try {
      setData(await api.updateExamItem(examId, itemId, patch));
    } catch (reason) {
      toast(errorMessage(reason), "error");
    }
  }

  async function regenerate(itemId: string) {
    try {
      setData(await api.regenerateExamItem(examId, itemId));
      toast("A new question was drafted from your materials.");
    } catch (reason) {
      toast(errorMessage(reason), "error");
    }
  }

  async function approveExam() {
    setApproving(true);
    try {
      setData(await api.approveExam(examId));
      toast("Exam approved. You can print it now.");
    } catch (reason) {
      toast(errorMessage(reason), "error");
    } finally {
      setApproving(false);
    }
  }

  if (exam.status === "generating") {
    return (
      <>
        <PageHeader title={exam.title} />
        <Panel>
          <p className="flex items-center gap-2 text-ink">
            <LoaderCircle className="size-5 animate-spin" aria-hidden />
            Drafting questions from this course&rsquo;s materials.
          </p>
          <p className="mt-1 text-ink-2">This takes a few seconds. The page updates on its own.</p>
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title={exam.title}
        description={
          exam.status === "approved"
            ? `Approved. ${plural(kept.length, "question")}, ${exam.totalPoints} points.`
            : `${plural(kept.length, "question")}, ${exam.totalPoints} points. ${pending} still to review.`
        }
        actions={
          <>
            <Link href="/professor/exams" className={buttonClass("quiet")}>
              All exams
            </Link>
            <Link
              href={`/print/exams/${exam.id}`}
              target="_blank"
              className={buttonClass("secondary")}
            >
              <Printer className="size-4" aria-hidden />
              Print view
            </Link>
            {exam.status !== "approved" ? (
              <Button
                variant="primary"
                busy={approving}
                disabled={pending > 0}
                title={pending > 0 ? "Approve or reject every question first." : undefined}
                onClick={() => void approveExam()}
              >
                Approve exam
              </Button>
            ) : null}
          </>
        }
      />

      {exam.status === "approved" ? (
        <p className="mb-5 flex items-center gap-2 rounded-md bg-good-wash px-4 py-3 text-good-ink">
          <Check className="size-5 shrink-0" aria-hidden />
          This exam is approved. Open the print view to hand it out, with or without the answer key.
        </p>
      ) : (
        <p className="mb-5 rounded-md bg-sunken px-4 py-3 text-sm text-ink-2">
          Every question was drafted from your uploaded materials and cites the passage it came from.
          Nothing is final until you approve it.
        </p>
      )}

      <ul className="space-y-4">
        {exam.items.map((item, i) => (
          <ItemCard
            key={item.id}
            item={item}
            position={i + 1}
            onPatch={(patch) => patchItem(item.id, patch)}
            onRegenerate={() => regenerate(item.id)}
          />
        ))}
      </ul>
    </>
  );
}

export default function ExamPage() {
  return (
    <Suspense fallback={<LoadingLines lines={6} label="Loading exam" />}>
      <ExamReview />
    </Suspense>
  );
}
