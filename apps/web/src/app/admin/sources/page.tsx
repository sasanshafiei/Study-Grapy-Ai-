"use client";

import { Play, Plus, RotateCcw } from "lucide-react";
import { useState, type FormEvent } from "react";
import { api, type Job, type Source } from "@/lib/api";
import { dateTime, errorMessage, plural } from "@/lib/format";
import { usePolling, useQuery } from "@/lib/use-query";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/field";
import { EmptyState, ErrorNote, LoadingLines } from "@/components/ui/feedback";
import { PageHeader, Panel } from "@/components/ui/page";
import { Status } from "@/components/ui/status";
import { useToast } from "@/components/ui/toast";

const JOB_KIND = {
  harvest: "Fetch from source",
  parse: "Read file",
  index: "Index",
} as const;

function lastRunCell(source: Source) {
  if (source.lastRun === "running") return <Status tone="busy">Running</Status>;
  if (source.lastRun === "never") return <Status tone="waiting">Not run yet</Status>;
  if (source.lastRun === "failed") return <Status tone="crit">Failed</Status>;
  return <Status tone="good">Succeeded</Status>;
}

function jobStateCell(job: Job) {
  if (job.state === "running") return <Status tone="busy">Running</Status>;
  if (job.state === "queued") return <Status tone="waiting">Queued</Status>;
  if (job.state === "failed") return <Status tone="crit">Failed</Status>;
  return <Status tone="good">Succeeded</Status>;
}

function AddSourceDialog({
  open,
  onClose,
  onAdded,
}: {
  open: boolean;
  onClose: () => void;
  onAdded: (source: Source) => void;
}) {
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("");
  const [licence, setLicence] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setName("");
    setDomain("");
    setLicence("");
    setError(null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const source = await api.createSource({ name, domain, licence });
      onAdded(source);
      reset();
      onClose();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
      title="Allow a new source"
    >
      <p className="mb-4 text-ink-2">
        StudyGraph only fetches pages from domains on this list.
      </p>
      <form onSubmit={submit} className="space-y-4" noValidate>
        <Field label="Name" htmlFor="source-name">
          <Input
            id="source-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="LibreTexts Chemistry"
            autoComplete="off"
            required
          />
        </Field>
        <Field label="Domain" htmlFor="source-domain" hint="Just the domain, with no https:// and no path.">
          <Input
            id="source-domain"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            placeholder="chem.libretexts.org"
            autoComplete="off"
            required
          />
        </Field>
        <Field
          label="Licence"
          htmlFor="source-licence"
          hint="Recorded against everything fetched from this domain."
        >
          <Input
            id="source-licence"
            value={licence}
            onChange={(e) => setLicence(e.target.value)}
            placeholder="CC BY-NC-SA 4.0"
            autoComplete="off"
          />
        </Field>

        {error ? (
          <p role="alert" className="rounded-md bg-crit-wash px-3 py-2 text-sm text-crit-ink">
            {error}
          </p>
        ) : null}

        <div className="flex justify-end gap-2 pt-1">
          <Button
            onClick={() => {
              reset();
              onClose();
            }}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={busy}>
            Allow this source
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

export default function SourcesPage() {
  const toast = useToast();
  const sources = useQuery("sources", () => api.listSources());
  const jobs = useQuery("jobs", () => api.listJobs());
  const [adding, setAdding] = useState(false);
  const [working, setWorking] = useState<string | null>(null);

  const busyRun = (jobs.data ?? []).some((j) => j.state === "running" || j.state === "queued");
  usePolling(busyRun, () => {
    jobs.reload();
    sources.reload();
  }, 1500);

  async function toggle(source: Source) {
    setWorking(source.id);
    try {
      const updated = await api.updateSource(source.id, { enabled: !source.enabled });
      sources.setData((sources.data ?? []).map((s) => (s.id === updated.id ? updated : s)));
      toast(
        updated.enabled
          ? `${updated.name} will be fetched on its schedule.`
          : `${updated.name} will not be fetched. Pages already indexed stay.`,
      );
    } catch (reason) {
      toast(errorMessage(reason), "error");
    } finally {
      setWorking(null);
    }
  }

  async function runNow(source: Source) {
    setWorking(source.id);
    try {
      await api.runSource(source.id);
      toast(`Fetching from ${source.name} now.`);
      jobs.reload();
      sources.reload();
    } catch (reason) {
      toast(errorMessage(reason), "error");
    } finally {
      setWorking(null);
    }
  }

  async function retry(job: Job) {
    setWorking(job.id);
    try {
      await api.retryJob(job.id);
      toast("Job restarted.");
      jobs.reload();
      sources.reload();
    } catch (reason) {
      toast(errorMessage(reason), "error");
    } finally {
      setWorking(null);
    }
  }

  if (sources.error) return <ErrorNote error={sources.error} onRetry={sources.reload} />;

  return (
    <>
      <PageHeader
        title="Sources"
        description="Where StudyGraph is allowed to fetch material from, on its own, to go with what professors upload."
        actions={
          <Button variant="primary" icon={<Plus className="size-4" aria-hidden />} onClick={() => setAdding(true)}>
            Allow a source
          </Button>
        }
      />

      <Panel flush className="mb-4">
        {!sources.data ? (
          <div className="p-5">
            <LoadingLines label="Loading sources" />
          </div>
        ) : sources.data.length === 0 ? (
          <div className="p-5">
            <EmptyState title="No sources allowed yet">
              Until a source is allowed, answers can only use what professors upload.
            </EmptyState>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-grid w-full">
              <caption className="sr-only">Allowed sources</caption>
              <thead>
                <tr>
                  <th scope="col">Source</th>
                  <th scope="col">Schedule</th>
                  <th scope="col">Last run</th>
                  <th scope="col" className="num">Documents</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {sources.data.map((source) => (
                  <tr key={source.id} className={working === source.id ? "opacity-60" : undefined}>
                    <td className="max-w-[20rem]">
                      <div className="font-semibold text-ink">{source.name}</div>
                      <div className="text-sm break-all text-ink-3">{source.domain}</div>
                      <div className="text-sm text-ink-3">{source.licence}</div>
                      {!source.enabled ? (
                        <div className="mt-1">
                          <Status tone="neutral">Turned off</Status>
                        </div>
                      ) : null}
                    </td>
                    <td className="text-ink-2">{source.schedule}</td>
                    <td>
                      {lastRunCell(source)}
                      {source.lastRunAt ? (
                        <div className="mt-1 text-sm text-ink-3">{dateTime(source.lastRunAt)}</div>
                      ) : null}
                      {source.error ? (
                        <div className="mt-1 max-w-[18rem] text-sm text-crit-ink">{source.error}</div>
                      ) : null}
                    </td>
                    <td className="num text-ink-2">{source.documents.toLocaleString("en-GB")}</td>
                    <td>
                      <div className="flex flex-wrap justify-end gap-2">
                        <Button
                          size="sm"
                          disabled={working === source.id || !source.enabled}
                          icon={<Play className="size-3.5" aria-hidden />}
                          onClick={() => void runNow(source)}
                        >
                          Run now
                        </Button>
                        <Button
                          size="sm"
                          variant={source.enabled ? "danger" : "secondary"}
                          disabled={working === source.id}
                          onClick={() => void toggle(source)}
                        >
                          {source.enabled ? "Turn off" : "Turn on"}
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title="Recent jobs" description="Fetching, reading and indexing work." flush>
        {!jobs.data ? (
          <div className="p-5">
            <LoadingLines label="Loading jobs" />
          </div>
        ) : jobs.data.length === 0 ? (
          <div className="p-5">
            <EmptyState title="No jobs yet">Jobs appear when a source runs or a file is uploaded.</EmptyState>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-grid w-full">
              <caption className="sr-only">Recent jobs</caption>
              <thead>
                <tr>
                  <th scope="col">Job</th>
                  <th scope="col">Status</th>
                  <th scope="col">Started</th>
                  <th scope="col">Result</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {jobs.data.map((job) => (
                  <tr key={job.id} className={working === job.id ? "opacity-60" : undefined}>
                    <td className="max-w-[20rem]">
                      <div className="font-semibold text-ink">{JOB_KIND[job.kind]}</div>
                      <div className="text-sm break-words text-ink-3">{job.subject}</div>
                    </td>
                    <td>{jobStateCell(job)}</td>
                    <td className="whitespace-nowrap text-ink-2">{dateTime(job.startedAt)}</td>
                    <td className="max-w-[20rem]">
                      {job.state === "succeeded" || job.state === "failed" ? (
                        <div className="text-sm text-ink-2">
                          {job.kind === "harvest"
                            ? `${plural(job.fetched, "page")} fetched, ${job.indexed} indexed, ${job.skipped} skipped`
                            : `${job.indexed} chunks indexed`}
                        </div>
                      ) : null}
                      {job.error ? <div className="mt-1 text-sm text-crit-ink">{job.error}</div> : null}
                    </td>
                    <td className="text-right">
                      {job.state === "failed" ? (
                        <Button
                          size="sm"
                          disabled={working === job.id}
                          icon={<RotateCcw className="size-3.5" aria-hidden />}
                          onClick={() => void retry(job)}
                        >
                          Retry
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <AddSourceDialog
        open={adding}
        onClose={() => setAdding(false)}
        onAdded={(source) => {
          sources.setData([...(sources.data ?? []), source]);
          toast(`${source.name} is now allowed.`);
        }}
      />
    </>
  );
}
