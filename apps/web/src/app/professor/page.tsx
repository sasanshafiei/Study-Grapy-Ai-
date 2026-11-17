"use client";

import { ClipboardList, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { api, type ItemStat } from "@/lib/api";
import { percent, plural } from "@/lib/format";
import { useQuery } from "@/lib/use-query";
import { MeterList } from "@/components/charts/charts";
import { useCourse } from "@/components/shell/app-shell";
import { buttonClass } from "@/components/ui/button";
import { EmptyState, ErrorNote, LoadingLines } from "@/components/ui/feedback";
import { PageHeader, Panel, StatRow, StatTile } from "@/components/ui/page";
import { Status } from "@/components/ui/status";

/**
 * Point-biserial discrimination: how well a question separates students who
 * did well overall from those who did not. Below 0.2 the question is not
 * telling the professor much.
 */
function discriminationNote(value: number) {
  if (value >= 0.4) return <Status tone="good">Separates well</Status>;
  if (value >= 0.2) return <Status tone="warn">Weak</Status>;
  return <Status tone="crit">Not separating</Status>;
}

function ItemTable({ items }: { items: ItemStat[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="table-grid w-full">
        <caption className="sr-only">Questions the class found hardest</caption>
        <thead>
          <tr>
            <th scope="col">Question</th>
            <th scope="col" className="num">Correct</th>
            <th scope="col" className="num">Answers</th>
            <th scope="col">Discrimination</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.questionId}>
              <td className="max-w-[32rem]">
                <div className="text-ink">{item.stem}</div>
                <div className="mt-0.5 text-sm text-ink-3">{item.topicName}</div>
              </td>
              <td className="num font-semibold text-ink">{percent(item.correctRate)}</td>
              <td className="num text-ink-2">{item.attempts}</td>
              <td>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold text-ink tabular-nums">
                    {item.discrimination.toFixed(2)}
                  </span>
                  {discriminationNote(item.discrimination)}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ProfessorHome() {
  const course = useCourse();
  const { data, error, loading, reload } = useQuery(`analytics:${course.id}`, () =>
    api.getClassAnalytics(course.id),
  );

  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data) return <LoadingLines lines={8} label="Loading class statistics" />;

  const weakest = [...data.topics].sort((a, b) => a.averageMastery - b.averageMastery);

  return (
    <div className={loading ? "opacity-60 transition-opacity" : "transition-opacity"}>
      <PageHeader
        title={course.name}
        description={
          weakest[0]
            ? `The class is weakest on ${weakest[0].topicName}, averaging ${percent(weakest[0].averageMastery)}.`
            : undefined
        }
        actions={
          <Link href="/professor/exams/new" className={buttonClass("primary")}>
            <ClipboardList className="size-4" aria-hidden />
            Build an exam
          </Link>
        }
      />

      <StatRow>
        <StatTile
          label="Students enrolled"
          value={String(data.studentCount)}
          note={`Join code ${course.joinCode}`}
        />
        <StatTile label="Active this week" value={String(data.activeThisWeek)} />
        <StatTile label="Questions answered" value={data.questionsAnswered.toLocaleString("en-GB")} />
        <StatTile label="Class accuracy" value={percent(data.classAccuracy)} />
      </StatRow>

      <p className="mt-4 flex items-start gap-2 rounded-md bg-sunken px-4 py-3 text-sm text-ink-2">
        <ShieldCheck className="mt-0.5 size-4 shrink-0" aria-hidden />
        <span className="min-w-0">
          Class averages only. Individual student results are never shown, and a topic is hidden until at
          least {data.minimumGroupSize} students have answered on it.
        </span>
      </p>

      <div className="mt-8">
        <Panel
          title="Where the class stands"
          description="Average mastery per topic, weakest first."
        >
          {data.hiddenForPrivacy ? (
            <EmptyState title="Not enough answers yet">
              Averages appear once at least {data.minimumGroupSize} students have practised.
            </EmptyState>
          ) : (
            <MeterList
              valueName="class average mastery"
              rows={weakest.map((topic) => ({
                id: topic.topicId,
                name: topic.topicName,
                value: topic.averageMastery,
                note: `${plural(topic.attempts, "answer")} from ${topic.studentsWithData} students`,
              }))}
            />
          )}
        </Panel>
      </div>

      <div className="mt-4">
        <Panel
          title="Hardest questions"
          description="Lowest correct rate first. Discrimination shows how well a question separates stronger students from weaker ones; below 0.20 it is not telling you much."
          flush
        >
          {data.hardestItems.length === 0 ? (
            <div className="p-5">
              <EmptyState title="No question statistics yet">
                These appear once students have practised.
              </EmptyState>
            </div>
          ) : (
            <ItemTable items={data.hardestItems} />
          )}
        </Panel>
      </div>
    </div>
  );
}
