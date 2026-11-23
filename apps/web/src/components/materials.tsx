"use client";

import { ExternalLink, Trash2, Upload } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";
import { api, type DocumentItem, type Origin } from "@/lib/api";
import { errorMessage, plural, relativeDay } from "@/lib/format";
import { useQuery, usePolling } from "@/lib/use-query";
import { useCourse } from "@/components/shell/app-shell";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState, ErrorNote, LoadingLines } from "@/components/ui/feedback";
import { PageHeader, Panel } from "@/components/ui/page";
import { OriginTag, Status } from "@/components/ui/status";
import { useToast } from "@/components/ui/toast";

const FILTERS: { value: Origin | "all"; label: string }[] = [
  { value: "all", label: "Everything" },
  { value: "class", label: "Class materials" },
  { value: "external", label: "Fetched sources" },
];

function statusCell(document: DocumentItem) {
  switch (document.status) {
    case "ready":
      return <Status tone="good">Ready</Status>;
    case "processing":
      return <Status tone="busy">Reading</Status>;
    case "queued":
      return <Status tone="waiting">Queued</Status>;
    case "failed":
      return <Status tone="crit">Failed</Status>;
  }
}

/**
 * The materials list, shared by the student and professor sections.
 *
 * `canManage` turns on uploading and removing. Students see the same list
 * read-only, so they can tell what the answers are drawing on.
 */
export function Materials({ canManage }: { canManage: boolean }) {
  const course = useCourse();
  const toast = useToast();
  const fileInput = useRef<HTMLInputElement>(null);

  const { data, error, reload, setData } = useQuery(`documents:${course.id}`, () =>
    api.listDocuments(course.id),
  );
  const [filter, setFilter] = useState<Origin | "all">("all");
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [toRemove, setToRemove] = useState<DocumentItem | null>(null);
  const [removing, setRemoving] = useState(false);

  // While a file is being read, the list refreshes so the status moves on.
  const working = (data ?? []).some((d) => d.status === "queued" || d.status === "processing");
  usePolling(working, reload, 1500);

  async function upload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setUploadError(null);
    const failures: string[] = [];
    for (const file of Array.from(files)) {
      try {
        await api.uploadDocument(course.id, file);
      } catch (reason) {
        failures.push(errorMessage(reason));
      }
    }
    setUploading(false);
    if (failures.length > 0) setUploadError(failures.join(" "));
    else toast(`${plural(files.length, "file")} uploaded. Reading ${files.length === 1 ? "it" : "them"} now.`);
    reload();
  }

  async function remove() {
    if (!toRemove) return;
    setRemoving(true);
    try {
      await api.deleteDocument(toRemove.id);
      setData((data ?? []).filter((d) => d.id !== toRemove.id));
      toast(`${toRemove.title} removed.`);
      setToRemove(null);
    } catch (reason) {
      toast(errorMessage(reason), "error");
    } finally {
      setRemoving(false);
    }
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    void upload(event.dataTransfer.files);
  }

  if (error) return <ErrorNote error={error} onRetry={reload} />;

  const documents = data ?? [];
  const shown = filter === "all" ? documents : documents.filter((d) => d.origin === filter);
  const classCount = documents.filter((d) => d.origin === "class").length;
  const fetchedCount = documents.filter((d) => d.origin !== "class").length;

  return (
    <>
      <PageHeader
        title="Materials"
        description={
          canManage
            ? "What this course can answer from. Upload your slides and notes; StudyGraph fetches open sources for the same topics on its own."
            : "What answers in this course are built from. Your professor uploads the class material; StudyGraph fetches the open sources."
        }
        actions={
          canManage ? (
            <Button
              variant="primary"
              busy={uploading}
              icon={<Upload className="size-4" aria-hidden />}
              onClick={() => fileInput.current?.click()}
            >
              Upload files
            </Button>
          ) : null
        }
      />

      {canManage ? (
        <>
          <input
            ref={fileInput}
            type="file"
            multiple
            accept=".pdf,.pptx,.docx,.txt,.md"
            className="sr-only"
            onChange={(e) => {
              void upload(e.target.files);
              e.target.value = "";
            }}
          />
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            className={`mb-5 rounded-lg border-2 border-dashed px-6 py-6 text-center transition-colors ${
              dragging ? "border-series bg-sunken" : "border-rule-strong"
            }`}
          >
            <p className="text-ink">
              Drop PDF, PPTX, DOCX, TXT or MD files here, or{" "}
              <button
                type="button"
                onClick={() => fileInput.current?.click()}
                className="font-semibold text-ink underline underline-offset-2"
              >
                choose files
              </button>
              .
            </p>
            <p className="mt-1 text-sm text-ink-3">Up to 25 MB each.</p>
          </div>
          {uploadError ? (
            <div className="mb-5">
              <ErrorNote error={new Error(uploadError)} />
            </div>
          ) : null}
        </>
      ) : null}

      <div className="mb-4 flex flex-wrap items-center gap-1 rounded-md bg-sunken p-1">
        {FILTERS.map((option) => {
          const active = option.value === filter;
          const n =
            option.value === "all" ? documents.length : option.value === "class" ? classCount : fetchedCount;
          return (
            <button
              key={option.value}
              type="button"
              aria-pressed={active}
              onClick={() => setFilter(option.value)}
              className={`rounded px-3 py-1.5 text-sm font-semibold transition-colors ${
                active ? "bg-surface text-ink shadow-sm" : "text-ink-2 hover:text-ink"
              }`}
            >
              {option.label} <span className="tabular-nums">({n})</span>
            </button>
          );
        })}
      </div>

      {filter === "external" ? (
        <p className="mb-4 rounded-md bg-sunken px-4 py-3 text-sm text-ink-2">
          These were fetched automatically from allowed open sources, matched to this course&rsquo;s topics.
          Nobody uploaded them. An admin controls which sources are used.
        </p>
      ) : null}

      <Panel flush>
        {!data ? (
          <div className="p-5">
            <LoadingLines label="Loading materials" />
          </div>
        ) : shown.length === 0 ? (
          <div className="p-5">
            <EmptyState title="Nothing here yet">
              {canManage
                ? "Upload a lecture PDF to get started. Fetched sources appear once a harvest has run."
                : "Your professor has not added materials for this filter yet."}
            </EmptyState>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-grid w-full">
              <caption className="sr-only">Course materials</caption>
              <thead>
                <tr>
                  <th scope="col">Title</th>
                  <th scope="col">Source</th>
                  <th scope="col">Status</th>
                  <th scope="col">Added</th>
                  {canManage ? (
                    <th scope="col">
                      <span className="sr-only">Actions</span>
                    </th>
                  ) : null}
                </tr>
              </thead>
              <tbody>
                {shown.map((document) => (
                  <tr key={document.id}>
                    <td className="max-w-[22rem]">
                      <div className="font-semibold break-words text-ink">{document.title}</div>
                      {document.pages ? (
                        <div className="text-sm text-ink-3">{plural(document.pages, "page")}</div>
                      ) : null}
                      {document.error ? (
                        <div className="mt-1 text-sm text-crit-ink">{document.error}</div>
                      ) : null}
                    </td>
                    <td>
                      <OriginTag origin={document.origin} />
                      {document.sourceName ? (
                        <div className="mt-1 text-sm text-ink-2">{document.sourceName}</div>
                      ) : null}
                      {document.licence ? (
                        <div className="text-sm text-ink-3">{document.licence}</div>
                      ) : null}
                      {document.url ? (
                        <a
                          href={document.url}
                          target="_blank"
                          rel="noreferrer noopener"
                          className="mt-0.5 inline-flex items-center gap-1 text-sm font-semibold text-ink underline underline-offset-2"
                        >
                          Open
                          <ExternalLink className="size-3" aria-hidden />
                        </a>
                      ) : null}
                    </td>
                    <td>{statusCell(document)}</td>
                    <td className="whitespace-nowrap text-ink-2">
                      <div>{relativeDay(document.addedAt)}</div>
                      {document.addedBy ? (
                        <div className="text-sm text-ink-3">by {document.addedBy}</div>
                      ) : null}
                    </td>
                    {canManage ? (
                      <td className="text-right">
                        {document.origin === "class" ? (
                          <Button
                            size="sm"
                            variant="danger"
                            icon={<Trash2 className="size-3.5" aria-hidden />}
                            onClick={() => setToRemove(document)}
                          >
                            Remove
                          </Button>
                        ) : null}
                      </td>
                    ) : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Dialog open={toRemove !== null} onClose={() => setToRemove(null)} title="Remove this file?">
        <p className="text-ink-2">
          <span className="font-semibold text-ink">{toRemove?.title}</span> will be removed from this
          course, and answers will stop citing it. This cannot be undone.
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <Button onClick={() => setToRemove(null)}>Keep it</Button>
          <Button variant="danger" busy={removing} onClick={() => void remove()}>
            Remove
          </Button>
        </div>
      </Dialog>
    </>
  );
}
