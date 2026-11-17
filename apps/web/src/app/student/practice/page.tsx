"use client";

import { CircleCheck, CircleX, Lightbulb, TrendingDown, TrendingUp } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useRef, useState } from "react";
import {
  api,
  type AttemptResult,
  type Difficulty,
  type Question,
  type Topic,
} from "@/lib/api";
import { percent, plural } from "@/lib/format";
import { useQuery } from "@/lib/use-query";
import { SourceCard } from "@/components/answer";
import { useCourse } from "@/components/shell/app-shell";
import { Button, buttonClass } from "@/components/ui/button";
import { Field, Select } from "@/components/ui/field";
import { ErrorNote, LoadingLines } from "@/components/ui/feedback";
import { PageHeader, Panel } from "@/components/ui/page";

const DIFFICULTIES: { value: Difficulty | "mixed"; label: string }[] = [
  { value: "mixed", label: "Mixed" },
  { value: "easy", label: "Easy" },
  { value: "medium", label: "Medium" },
  { value: "hard", label: "Hard" },
];

interface Answered {
  question: Question;
  result: AttemptResult;
}

// --- Setup ------------------------------------------------------------------

function Setup({
  topics,
  chosen,
  onToggle,
  count,
  setCount,
  difficulty,
  setDifficulty,
  onStart,
  busy,
  error,
}: {
  topics: Topic[];
  chosen: Set<string>;
  onToggle: (id: string) => void;
  count: number;
  setCount: (n: number) => void;
  difficulty: Difficulty | "mixed";
  setDifficulty: (d: Difficulty | "mixed") => void;
  onStart: () => void;
  busy: boolean;
  error: unknown;
}) {
  return (
    <Panel title="Build a practice set" description="Leave every topic unticked to practise all of them.">
      <fieldset>
        <legend className="mb-2 text-sm font-semibold text-ink">Topics</legend>
        <ul className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
          {topics.map((topic) => (
            <li key={topic.id}>
              <label className="flex items-center gap-2.5 text-ink">
                <input
                  type="checkbox"
                  checked={chosen.has(topic.id)}
                  onChange={() => onToggle(topic.id)}
                  className="size-4 accent-[var(--series)]"
                />
                {topic.name}
              </label>
            </li>
          ))}
        </ul>
      </fieldset>

      <div className="mt-5 grid gap-4 sm:grid-cols-2">
        <Field label="How many questions" htmlFor="count">
          <Select id="count" value={count} onChange={(e) => setCount(Number(e.target.value))}>
            {[5, 10, 15, 20].map((n) => (
              <option key={n} value={n}>
                {n} questions
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Difficulty" htmlFor="difficulty">
          <Select
            id="difficulty"
            value={difficulty}
            onChange={(e) => setDifficulty(e.target.value as Difficulty | "mixed")}
          >
            {DIFFICULTIES.map((d) => (
              <option key={d.value} value={d.value}>
                {d.label}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      {error ? (
        <div className="mt-4">
          <ErrorNote error={error} />
        </div>
      ) : null}

      <div className="mt-5">
        <Button variant="primary" busy={busy} onClick={onStart}>
          Start practising
        </Button>
      </div>
    </Panel>
  );
}

// --- Feedback after an answer ------------------------------------------------

function Feedback({ result }: { result: AttemptResult }) {
  const change = result.mastery.mastery - result.masteryBefore;
  const up = change >= 0;
  return (
    <div
      className={`mt-5 rounded-lg border p-4 ${
        result.correct ? "border-good/40 bg-good-wash" : "border-crit/40 bg-crit-wash"
      }`}
    >
      <p
        className={`flex items-center gap-2 font-bold ${result.correct ? "text-good-ink" : "text-crit-ink"}`}
      >
        {result.correct ? (
          <CircleCheck className="size-5" aria-hidden />
        ) : (
          <CircleX className="size-5" aria-hidden />
        )}
        {result.correct ? "Correct" : "Not quite. The right answer is marked above."}
      </p>
      <p className="reading mt-2 text-ink">{result.explanation}</p>

      <p className="mt-3 flex items-center gap-1.5 text-sm font-semibold text-ink">
        {up ? (
          <TrendingUp className="size-4 text-good-ink" aria-hidden />
        ) : (
          <TrendingDown className="size-4 text-crit-ink" aria-hidden />
        )}
        {result.mastery.topicName} mastery {percent(result.masteryBefore)} &rarr;{" "}
        {percent(result.mastery.mastery)}
      </p>

      <ul className="mt-3">
        <SourceCard citation={result.citation} />
      </ul>
    </div>
  );
}

// --- Summary ----------------------------------------------------------------

function Summary({ answered, onAgain }: { answered: Answered[]; onAgain: () => void }) {
  const correct = answered.filter((a) => a.result.correct).length;
  const byTopic = new Map<string, { name: string; before: number; after: number; total: number; right: number }>();
  for (const { result } of answered) {
    const row = byTopic.get(result.mastery.topicId);
    if (row) {
      row.after = result.mastery.mastery;
      row.total += 1;
      row.right += result.correct ? 1 : 0;
    } else {
      byTopic.set(result.mastery.topicId, {
        name: result.mastery.topicName,
        before: result.masteryBefore,
        after: result.mastery.mastery,
        total: 1,
        right: result.correct ? 1 : 0,
      });
    }
  }

  return (
    <Panel title="Practice finished">
      <p className="text-[26px] font-bold text-ink">
        {correct} of {answered.length} correct
      </p>
      <p className="mt-1 text-ink-2">{percent(correct / answered.length)} on this set.</p>

      <table className="table-grid mt-5 w-full">
        <caption className="sr-only">Mastery change by topic</caption>
        <thead>
          <tr>
            <th scope="col">Topic</th>
            <th scope="col" className="num">Correct</th>
            <th scope="col" className="num">Mastery</th>
          </tr>
        </thead>
        <tbody>
          {[...byTopic.entries()].map(([id, row]) => (
            <tr key={id}>
              <td className="font-semibold text-ink">{row.name}</td>
              <td className="num">{row.right} of {row.total}</td>
              <td className="num">
                <span className="text-ink-3">{percent(row.before)}</span>
                <span aria-hidden> &rarr; </span>
                <span className="sr-only">to</span>
                <span className="font-semibold text-ink">{percent(row.after)}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="mt-5 flex flex-wrap gap-2">
        <Button variant="primary" onClick={onAgain}>
          Practise again
        </Button>
        <Link href="/student" className={buttonClass("secondary")}>
          Back to today
        </Link>
      </div>
    </Panel>
  );
}

// --- The screen --------------------------------------------------------------

function Practice() {
  const course = useCourse();
  const params = useSearchParams();
  const preselected = params.get("topic");

  const topics = useQuery(`topics:${course.id}`, () => api.listTopics(course.id));

  // A topic can arrive in the URL from a dashboard recommendation.
  const [chosen, setChosen] = useState<Set<string>>(() =>
    preselected ? new Set([preselected]) : new Set(),
  );
  const [count, setCount] = useState(5);
  const [difficulty, setDifficulty] = useState<Difficulty | "mixed">("mixed");

  const [questions, setQuestions] = useState<Question[] | null>(null);
  const [index, setIndex] = useState(0);
  const [picked, setPicked] = useState<string | null>(null);
  const [hintShown, setHintShown] = useState(false);
  /** When the current question appeared, for the response time sent with an attempt. */
  const startedAt = useRef(0);
  const [result, setResult] = useState<AttemptResult | null>(null);
  const [answered, setAnswered] = useState<Answered[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState(false);

  function toggle(id: string) {
    setChosen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const set = await api.generateQuiz(course.id, {
        topicIds: [...chosen],
        count,
        difficulty,
      });
      setQuestions(set);
      setIndex(0);
      setPicked(null);
      setHintShown(false);
      setResult(null);
      setAnswered([]);
      setDone(false);
      startedAt.current = Date.now();
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  }

  async function check() {
    if (!questions || picked === null) return;
    setBusy(true);
    setError(null);
    try {
      const outcome = await api.submitAttempt(course.id, {
        questionId: questions[index].id,
        answer: picked,
        hintCount: hintShown ? 1 : 0,
        responseMs: Date.now() - startedAt.current,
      });
      setResult(outcome);
      setAnswered((list) => [...list, { question: questions[index], result: outcome }]);
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  }

  function next() {
    if (!questions) return;
    if (index + 1 >= questions.length) {
      setDone(true);
      return;
    }
    setIndex((i) => i + 1);
    setPicked(null);
    setHintShown(false);
    setResult(null);
    startedAt.current = Date.now();
  }

  if (topics.error) return <ErrorNote error={topics.error} onRetry={topics.reload} />;
  if (!topics.data) return <LoadingLines label="Loading topics" />;

  if (done) {
    return (
      <>
        <PageHeader title="Practice" />
        <Summary answered={answered} onAgain={() => { setQuestions(null); setDone(false); }} />
      </>
    );
  }

  if (!questions) {
    return (
      <>
        <PageHeader
          title="Practice"
          description="Questions are generated from this course's materials, and every answer cites where it came from."
        />
        <Setup
          topics={topics.data}
          chosen={chosen}
          onToggle={toggle}
          count={count}
          setCount={setCount}
          difficulty={difficulty}
          setDifficulty={setDifficulty}
          onStart={() => void start()}
          busy={busy}
          error={error}
        />
      </>
    );
  }

  const question = questions[index];
  const progress = (index + (result ? 1 : 0)) / questions.length;

  return (
    <>
      <PageHeader
        title="Practice"
        description={`Question ${index + 1} of ${questions.length}`}
        actions={
          <Button variant="quiet" onClick={() => { setQuestions(null); setAnswered([]); }}>
            End set
          </Button>
        }
      />

      <div
        role="progressbar"
        aria-label="Progress through this set"
        aria-valuemin={0}
        aria-valuemax={questions.length}
        aria-valuenow={index + (result ? 1 : 0)}
        className="mb-5 h-1.5 overflow-hidden rounded-full bg-series-track"
      >
        <div className="h-full rounded-r-full bg-series transition-[width]" style={{ width: `${progress * 100}%` }} />
      </div>

      <Panel>
        <p className="text-sm font-semibold text-ink-3">
          {question.topicName} · {question.difficulty}
        </p>
        <h2 className="reading mt-2 font-semibold text-ink">{question.stem}</h2>

        <fieldset className="mt-4" disabled={result !== null}>
          <legend className="sr-only">Choose an answer</legend>
          <ul className="space-y-2">
            {question.choices.map((choice) => {
              const isPicked = picked === choice;
              const isRight = result?.correctAnswer === choice;
              const wrongPick = result !== null && isPicked && !result.correct;
              return (
                <li key={choice}>
                  <label
                    className={`flex cursor-pointer items-start gap-3 rounded-md border p-3 transition-colors ${
                      result !== null && isRight
                        ? "border-good bg-good-wash"
                        : wrongPick
                          ? "border-crit bg-crit-wash"
                          : isPicked
                            ? "border-rule-strong bg-sunken"
                            : "border-rule hover:bg-sunken"
                    }`}
                  >
                    <input
                      type="radio"
                      name="choice"
                      value={choice}
                      checked={isPicked}
                      onChange={() => setPicked(choice)}
                      className="mt-1 size-4 accent-[var(--series)]"
                    />
                    <span className="min-w-0 text-ink">{choice}</span>
                  </label>
                </li>
              );
            })}
          </ul>
        </fieldset>

        {hintShown && !result ? (
          <p className="mt-4 flex gap-2 rounded-md bg-warn-wash px-3 py-2 text-warn-ink">
            <Lightbulb className="mt-0.5 size-4 shrink-0" aria-hidden />
            <span className="min-w-0">{question.hint}</span>
          </p>
        ) : null}

        {error ? (
          <div className="mt-4">
            <ErrorNote error={error} />
          </div>
        ) : null}

        {result ? <Feedback result={result} /> : null}

        <div className="mt-5 flex flex-wrap gap-2">
          {result ? (
            <Button variant="primary" onClick={next}>
              {index + 1 >= questions.length ? "See results" : "Next question"}
            </Button>
          ) : (
            <>
              <Button variant="primary" busy={busy} disabled={picked === null} onClick={() => void check()}>
                Check answer
              </Button>
              {!hintShown && question.hint ? (
                <Button
                  variant="secondary"
                  icon={<Lightbulb className="size-4" aria-hidden />}
                  onClick={() => setHintShown(true)}
                >
                  Show a hint
                </Button>
              ) : null}
            </>
          )}
        </div>
      </Panel>

      {answered.length > 0 ? (
        <p className="mt-4 text-sm text-ink-3">
          {plural(answered.filter((a) => a.result.correct).length, "correct answer")} so far.
        </p>
      ) : null}
    </>
  );
}

export default function PracticePage() {
  return (
    <Suspense fallback={<LoadingLines label="Loading practice" />}>
      <Practice />
    </Suspense>
  );
}
