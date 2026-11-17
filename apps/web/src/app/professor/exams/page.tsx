"use client";

import { ClipboardList } from "lucide-react";
import Link from "next/link";
import { api, type ExamSummary } from "@/lib/api";
import { plural, relativeDay } from "@/lib/format";
import { usePolling, useQuery } from "@/lib/use-query";
import { useCourse } from "@/components/shell/app-shell";
import { buttonClass } from "@/components/ui/button";
import { EmptyState, ErrorNote, LoadingLines } from "@/components/ui/feedback";
import { PageHeader, Panel } from "@/components/ui/page";
import { Status } from "@/components/ui/status";

function statusCell(exam: ExamSummary) {
  if (exam.status === "generating") return <Status tone="busy">Writing questions</Status>;
  if (exam.status === "approved") return <Status tone="good">Approved</Status>;
  const left = exam.questionCount - exam.approvedCount;
  return <Status tone="warn">{plural(left, "question")} to review</Status>;
}

export default function ExamsPage() {
  const course = useCourse();
  const { data, error, reload } = useQuery(`exams:${course.id}`, () => api.listExams(course.id));

  usePolling((data ?? []).some((e) => e.status === "generating"), reload, 1500);

  if (error) return <ErrorNote error={error} onRetry={reload} />;

  return (
    <>
      <PageHeader
        title="Exams"
        description="Questions drafted from this course's materials. You approve every one before it counts."
        actions={
          <Link href="/professor/exams/new" className={buttonClass("primary")}>
            <ClipboardList className="size-4" aria-hidden />
            Build an exam
          </Link>
        }
      />

      <Panel flush>
        {!data ? (
          <div className="p-5">
            <LoadingLines label="Loading exams" />
          </div>
        ) : data.length === 0 ? (
          <div className="p-5">
            <EmptyState
              title="No exams yet"
              action={
                <Link href="/professor/exams/new" className={buttonClass("primary")}>
                  Build your first exam
                </Link>
              }
            >
              Choose the topics and how many questions you want, and StudyGraph drafts them from your
              uploaded materials.
            </EmptyState>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-grid w-full">
              <caption className="sr-only">Exams in this course</caption>
              <thead>
                <tr>
                  <th scope="col">Exam</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="num">Questions</th>
                  <th scope="col" className="num">Points</th>
                  <th scope="col">Created</th>
                </tr>
              </thead>
              <tbody>
                {data.map((exam) => (
                  <tr key={exam.id}>
                    <td>
                      <Link
                        href={`/professor/exams/${exam.id}`}
                        className="font-semibold text-ink underline underline-offset-2"
                      >
                        {exam.title}
                      </Link>
                    </td>
                    <td>{statusCell(exam)}</td>
                    <td className="num text-ink-2">{exam.questionCount}</td>
                    <td className="num text-ink-2">{exam.totalPoints}</td>
                    <td className="whitespace-nowrap text-ink-2">{relativeDay(exam.createdAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
