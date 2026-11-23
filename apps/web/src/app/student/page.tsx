"use client";

import {
  ExternalLink,
  FileText,
  ListChecks,
  RotateCcw,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import Link from "next/link";
import { api, type Recommendation, type TopicMastery } from "@/lib/api";
import { minutes, percent, plural, relativeDay, shortDate, weekday } from "@/lib/format";
import { useQuery } from "@/lib/use-query";
import { useCourse } from "@/components/shell/app-shell";
import { ChartPanel, Columns, MeterList, TrendLine, type ChartPoint } from "@/components/charts/charts";
import { buttonClass } from "@/components/ui/button";
import { EmptyState, ErrorNote, LoadingLines } from "@/components/ui/feedback";
import { PageHeader, Panel, StatRow, StatTile } from "@/components/ui/page";
import { OriginTag } from "@/components/ui/status";

const ICON = {
  practice_quiz: ListChecks,
  review_topic: RotateCcw,
  class_material: FileText,
  external_resource: ExternalLink,
} as const;

function RecommendationCard({ item }: { item: Recommendation }) {
  const Icon = ICON[item.type];
  const external = item.type === "external_resource" && item.url;
  const href = external
    ? item.url!
    : item.type === "class_material"
      ? "/student/materials"
      : `/student/practice?topic=${item.topicId}`;
  const action = external
    ? "Open"
    : item.type === "class_material"
      ? "Find it"
      : item.type === "review_topic"
        ? "Review"
        : "Start";

  return (
    <li className="flex flex-col gap-3 rounded-lg border border-rule bg-surface p-4 sm:flex-row sm:items-center">
      <Icon className="size-5 shrink-0 text-ink-2" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="font-semibold text-ink">{item.title}</p>
        <p className="mt-0.5 text-sm text-ink-2">{item.reason}</p>
        <p className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-ink-3">
          {item.origin ? <OriginTag origin={item.origin} /> : null}
          {/* The source is only worth naming when it is not already the title. */}
          {item.sourceTitle && item.sourceTitle !== item.title ? (
            <span className="min-w-0 break-words">{item.sourceTitle}</span>
          ) : null}
          <span>About {minutes(item.minutes)}</span>
        </p>
      </div>
      {external ? (
        <a
          href={href}
          target="_blank"
          rel="noreferrer noopener"
          className={buttonClass("secondary", "sm", "shrink-0")}
        >
          {action}
          <ExternalLink className="size-3.5" aria-hidden />
        </a>
      ) : (
        <Link href={href} className={buttonClass("secondary", "sm", "shrink-0")}>
          {action}
        </Link>
      )}
    </li>
  );
}

function trendFlag(row: TopicMastery) {
  if (row.trend === "up") {
    return (
      <span className="inline-flex items-center gap-1 text-sm font-semibold text-good-ink">
        <TrendingUp className="size-3.5" aria-hidden />
        Improving
      </span>
    );
  }
  if (row.trend === "down") {
    return (
      <span className="inline-flex items-center gap-1 text-sm font-semibold text-crit-ink">
        <TrendingDown className="size-3.5" aria-hidden />
        Slipping
      </span>
    );
  }
  return null;
}

export default function StudentHome() {
  const course = useCourse();
  const { data, error, loading, reload } = useQuery(`dashboard:${course.id}`, () =>
    api.getStudentDashboard(course.id),
  );

  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data) return <LoadingLines lines={8} label="Loading your progress" />;

  const { stats, mastery, recommendations, accuracyByDay, minutesByDay } = data;
  const weakest = [...mastery].sort((a, b) => a.mastery - b.mastery)[0];

  const accuracyPoints: ChartPoint[] = accuracyByDay.map((d) => ({
    label: shortDate(d.date),
    longLabel: `${weekday(d.date)} ${shortDate(d.date)}`,
    value: d.accuracy,
    detail: d.attempts > 0 ? plural(d.attempts, "answer") : "No practice",
  }));
  const minutePoints: ChartPoint[] = minutesByDay.map((d) => ({
    label: shortDate(d.date),
    longLabel: `${weekday(d.date)} ${shortDate(d.date)}`,
    value: d.minutes,
  }));

  return (
    <div className={loading ? "opacity-60 transition-opacity" : "transition-opacity"}>
      <PageHeader
        title="Today"
        description={
          weakest
            ? `${weakest.topicName} is your weakest topic right now, at ${percent(weakest.mastery)}.`
            : "Start practising to see where you stand."
        }
      />

      <StatRow>
        <StatTile label="Questions answered" value={stats.questionsAnswered.toLocaleString("en-GB")} />
        <StatTile label="Accuracy this week" value={percent(stats.accuracy)} />
        <StatTile
          label="Study streak"
          value={plural(stats.streakDays, "day")}
          note={stats.streakDays === 0 ? "Answer one question to start" : undefined}
        />
        <StatTile label="Time this week" value={minutes(stats.minutesThisWeek)} />
      </StatRow>

      <section className="mt-8">
        <h2 className="text-[17px] font-bold text-ink">What to do next</h2>
        <p className="mt-0.5 text-ink-2">Based on the questions you have been getting wrong.</p>
        {recommendations.length === 0 ? (
          <div className="mt-3">
            <EmptyState title="Nothing to recommend yet">
              Answer a few practice questions and recommendations will appear here.
            </EmptyState>
          </div>
        ) : (
          <ul className="mt-3 space-y-2">
            {recommendations.map((item) => (
              <RecommendationCard key={item.id} item={item} />
            ))}
          </ul>
        )}
      </section>

      <div className="mt-8 grid gap-4 lg:grid-cols-2">
        <ChartPanel
          title="Accuracy"
          description="Share of practice answers correct, last 14 days."
          points={accuracyPoints}
          valueHeading="Accuracy"
          format={percent}
          emptyValue="No practice"
        >
          <TrendLine
            points={accuracyPoints}
            format={percent}
            max={1}
            emptyValue="No practice"
            label="Accuracy over the last 14 days"
          />
        </ChartPanel>

        <ChartPanel
          title="Time spent"
          description="Minutes of practice per day, last 14 days."
          points={minutePoints}
          valueHeading="Minutes"
          format={(v) => `${Math.round(v)}`}
        >
          <Columns
            points={minutePoints}
            format={(v) => `${Math.round(v)}`}
            label="Minutes of practice per day over the last 14 days"
          />
        </ChartPanel>
      </div>

      <div className="mt-4">
        <Panel
          title="Topic mastery"
          description="How well you know each topic, from your practice answers."
        >
          <MeterList
            valueName="mastery"
            rows={mastery.map((row) => ({
              id: row.topicId,
              name: row.topicName,
              value: row.mastery,
              note: `${plural(row.attempts, "answer")}${
                row.nextReviewAt ? ` · Review ${relativeDay(row.nextReviewAt).toLowerCase()}` : ""
              }`,
              flag: trendFlag(row),
            }))}
          />
        </Panel>
      </div>
    </div>
  );
}
