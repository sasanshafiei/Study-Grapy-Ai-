"use client";

import Link from "next/link";
import { api, type HealthState } from "@/lib/api";
import { compact, shortDate, weekday } from "@/lib/format";
import { usePolling, useQuery } from "@/lib/use-query";
import { ChartPanel, Columns, type ChartPoint } from "@/components/charts/charts";
import { buttonClass } from "@/components/ui/button";
import { ErrorNote, LoadingLines } from "@/components/ui/feedback";
import { PageHeader, Panel, StatRow, StatTile } from "@/components/ui/page";
import { Status, type Tone } from "@/components/ui/status";

const HEALTH: Record<HealthState, { tone: Tone; label: string }> = {
  ok: { tone: "good", label: "Healthy" },
  degraded: { tone: "warn", label: "Degraded" },
  down: { tone: "crit", label: "Down" },
};

export default function AdminHome() {
  const { data, error, loading, reload } = useQuery("overview", () => api.getSystemOverview());

  // Keep the health view current while someone is watching it.
  usePolling(Boolean(data), reload, 10_000);

  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data) return <LoadingLines lines={8} label="Loading system status" />;

  const requests: ChartPoint[] = data.usageByDay.map((d) => ({
    label: shortDate(d.date),
    longLabel: `${weekday(d.date)} ${shortDate(d.date)}`,
    value: d.requests,
    detail: `${compact(d.tokens)} tokens`,
  }));

  const worst = data.services.find((s) => s.state !== "ok");

  return (
    <div className={loading ? "opacity-60 transition-opacity" : "transition-opacity"}>
      <PageHeader
        title="System overview"
        description={
          worst
            ? `${worst.name}: ${worst.detail.toLowerCase()}.`
            : "Everything is running normally."
        }
        actions={
          <Link href="/admin/sources" className={buttonClass("secondary")}>
            Sources and jobs
          </Link>
        }
      />

      <StatRow>
        <StatTile label="Accounts" value={String(data.counts.users)} />
        <StatTile label="Courses" value={String(data.counts.courses)} />
        <StatTile label="Documents indexed" value={compact(data.counts.documentsReady)} />
        <StatTile
          label="Failed jobs"
          value={String(data.counts.failedJobs)}
          note={data.counts.failedJobs > 0 ? "Needs attention" : undefined}
        />
      </StatRow>

      <div className="mt-8 grid gap-4 lg:grid-cols-[minmax(0,20rem)_1fr]">
        <Panel title="Services">
          <ul className="divide-y divide-rule">
            {data.services.map((service) => (
              <li key={service.name} className="flex items-start justify-between gap-3 py-3 first:pt-0 last:pb-0">
                <div className="min-w-0">
                  <p className="font-semibold text-ink">{service.name}</p>
                  <p className="text-sm text-ink-2">{service.detail}</p>
                </div>
                <Status tone={HEALTH[service.state].tone}>{HEALTH[service.state].label}</Status>
              </li>
            ))}
          </ul>
        </Panel>

        <ChartPanel
          title="Model requests"
          description="Calls to the AI service per day, last 14 days."
          points={requests}
          valueHeading="Requests"
          format={(v) => compact(Math.round(v))}
        >
          <Columns
            points={requests}
            format={(v) => compact(Math.round(v))}
            label="Model requests per day over the last 14 days"
          />
        </ChartPanel>
      </div>
    </div>
  );
}
