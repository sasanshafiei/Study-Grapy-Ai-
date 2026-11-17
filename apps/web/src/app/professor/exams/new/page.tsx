"use client";

import { Plus, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import {
  api,
  type BlueprintRow,
  type Difficulty,
  type QuestionType,
} from "@/lib/api";
import { plural } from "@/lib/format";
import { useQuery } from "@/lib/use-query";
import { useCourse } from "@/components/shell/app-shell";
import { Button } from "@/components/ui/button";
import { Field, Input, Select } from "@/components/ui/field";
import { ErrorNote, LoadingLines } from "@/components/ui/feedback";
import { PageHeader, Panel } from "@/components/ui/page";

const TYPES: { value: QuestionType; label: string; points: number }[] = [
  { value: "mcq", label: "Multiple choice", points: 5 },
  { value: "true_false", label: "True or false", points: 2 },
  { value: "short", label: "Short answer", points: 10 },
];

const DIFFICULTIES: Difficulty[] = ["easy", "medium", "hard"];

export default function NewExamPage() {
  const course = useCourse();
  const router = useRouter();
  const topics = useQuery(`topics:${course.id}`, () => api.listTopics(course.id));

  const [title, setTitle] = useState("");
  const [rows, setRows] = useState<BlueprintRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [titleError, setTitleError] = useState<string | null>(null);

  const total = rows.reduce((n, r) => n + r.count, 0);
  const points = rows.reduce((n, r) => n + r.count * r.pointsEach, 0);

  function addRow() {
    const firstTopic = topics.data?.[0];
    if (!firstTopic) return;
    setRows((current) => [
      ...current,
      { topicId: firstTopic.id, type: "mcq", difficulty: "medium", count: 5, pointsEach: 5 },
    ]);
  }

  function patchRow(index: number, patch: Partial<BlueprintRow>) {
    setRows((current) =>
      current.map((row, i) => {
        if (i !== index) return row;
        const next = { ...row, ...patch };
        // Changing the question type resets points to that type's default.
        if (patch.type && patch.type !== row.type) {
          next.pointsEach = TYPES.find((t) => t.value === patch.type)?.points ?? row.pointsEach;
        }
        return next;
      }),
    );
  }

  async function create() {
    if (!title.trim()) {
      setTitleError("Give the exam a name, so you can find it later.");
      return;
    }
    if (rows.length === 0) {
      setError(new Error("Add at least one line to the blueprint."));
      return;
    }
    setBusy(true);
    setError(null);
    setTitleError(null);
    try {
      const exam = await api.createExam(course.id, title, rows);
      router.push(`/professor/exams/${exam.id}`);
    } catch (reason) {
      setError(reason);
      setBusy(false);
    }
  }

  if (topics.error) return <ErrorNote error={topics.error} onRetry={topics.reload} />;
  if (!topics.data) return <LoadingLines label="Loading topics" />;

  return (
    <>
      <PageHeader
        title="Build an exam"
        description="Say what the exam should cover. StudyGraph drafts the questions from this course's materials, and you review every one before it counts."
      />

      <div className="space-y-4">
        <Panel>
          <Field
            label="Exam name"
            htmlFor="title"
            hint="Students never see this; it is how you find the exam later."
            error={titleError}
          >
            <Input
              id="title"
              value={title}
              onChange={(e) => {
                setTitle(e.target.value);
                setTitleError(null);
              }}
              placeholder="Midterm 2: Thermochemistry and Equilibrium"
            />
          </Field>
        </Panel>

        <Panel
          title="Blueprint"
          description="One line per group of questions."
          actions={
            <Button size="sm" icon={<Plus className="size-4" aria-hidden />} onClick={addRow}>
              Add a line
            </Button>
          }
        >
          {rows.length === 0 ? (
            <p className="rounded-lg border border-dashed border-rule-strong px-5 py-8 text-center text-ink-2">
              No lines yet. Add one to say which topic to cover and how many questions to write.
            </p>
          ) : (
            <ul className="space-y-3">
              {rows.map((row, index) => (
                <li
                  key={index}
                  className="grid gap-3 rounded-lg border border-rule p-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1.4fr)_minmax(0,1fr)_5.5rem_5.5rem_auto]"
                >
                  <Field label="Topic" htmlFor={`topic-${index}`}>
                    <Select
                      id={`topic-${index}`}
                      value={row.topicId}
                      onChange={(e) => patchRow(index, { topicId: e.target.value })}
                    >
                      {topics.data!.map((topic) => (
                        <option key={topic.id} value={topic.id}>
                          {topic.name}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="Type" htmlFor={`type-${index}`}>
                    <Select
                      id={`type-${index}`}
                      value={row.type}
                      onChange={(e) => patchRow(index, { type: e.target.value as QuestionType })}
                    >
                      {TYPES.map((type) => (
                        <option key={type.value} value={type.value}>
                          {type.label}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="Difficulty" htmlFor={`difficulty-${index}`}>
                    <Select
                      id={`difficulty-${index}`}
                      value={row.difficulty}
                      onChange={(e) => patchRow(index, { difficulty: e.target.value as Difficulty })}
                    >
                      {DIFFICULTIES.map((d) => (
                        <option key={d} value={d}>
                          {d[0].toUpperCase() + d.slice(1)}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="How many" htmlFor={`count-${index}`}>
                    <Input
                      id={`count-${index}`}
                      type="number"
                      min={1}
                      max={25}
                      value={row.count}
                      onChange={(e) =>
                        patchRow(index, { count: Math.max(1, Math.min(25, Number(e.target.value) || 1)) })
                      }
                    />
                  </Field>
                  <Field label="Points each" htmlFor={`points-${index}`}>
                    <Input
                      id={`points-${index}`}
                      type="number"
                      min={1}
                      max={50}
                      value={row.pointsEach}
                      onChange={(e) =>
                        patchRow(index, {
                          pointsEach: Math.max(1, Math.min(50, Number(e.target.value) || 1)),
                        })
                      }
                    />
                  </Field>
                  <div className="flex items-end">
                    <Button
                      variant="danger"
                      aria-label={`Remove line ${index + 1}`}
                      icon={<Trash2 className="size-4" aria-hidden />}
                      onClick={() => setRows((current) => current.filter((_, i) => i !== index))}
                    />
                  </div>
                </li>
              ))}
            </ul>
          )}

          {rows.length > 0 ? (
            <p className="mt-4 border-t border-rule pt-3 font-semibold text-ink">
              {plural(total, "question")}, {points} points in total.
            </p>
          ) : null}
        </Panel>

        {error ? <ErrorNote error={error} /> : null}

        <div className="flex flex-wrap gap-2">
          <Button variant="primary" busy={busy} disabled={rows.length === 0} onClick={() => void create()}>
            Draft the questions
          </Button>
          <Button onClick={() => router.push("/professor/exams")}>Cancel</Button>
        </div>
      </div>
    </>
  );
}
