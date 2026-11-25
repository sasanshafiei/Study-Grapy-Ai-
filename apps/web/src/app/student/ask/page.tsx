"use client";

import { ListChecks, LoaderCircle, PanelLeft, Send } from "lucide-react";
import Link from "next/link";
import { useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api, type ChatMessage, type Citation, type Scope } from "@/lib/api";
import { SUGGESTED_QUESTIONS } from "@/lib/api/mock-data";
import { plural } from "@/lib/format";
import { useQuery } from "@/lib/use-query";
import { AnswerText, NotGroundedNote } from "@/components/answer";
import { Inspector, CitationDetail } from "@/components/ask/inspector";
import { SourceRail, SourceSummary } from "@/components/ask/source-rail";
import { useCourse } from "@/components/shell/app-shell";
import { Button, buttonClass } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ErrorNote } from "@/components/ui/feedback";
import { Textarea } from "@/components/ui/field";
import { ORIGIN } from "@/components/ui/status";

/**
 * The Ask workspace.
 *
 * Three panes: what the answer may draw on, the conversation, and the passage
 * behind whichever citation you clicked. The middle pane is the only one that
 * scrolls with the conversation; the other two hold their own scroll, so a
 * reader never loses their place.
 *
 * Below xl the same three pieces become one column plus two dialogs.
 */

function Answer({
  message,
  onInspect,
  activeMarker,
  topicName,
}: {
  message: ChatMessage;
  onInspect: (citation: Citation) => void;
  activeMarker: number | null;
  topicName: string | null;
}) {
  return (
    <article className="rounded-lg border border-rule bg-surface p-5">
      {message.grounded ? (
        <AnswerText
          text={message.text}
          citations={message.citations}
          onCitationClick={(marker) => {
            const citation = message.citations.find((c) => c.marker === marker);
            if (citation) onInspect(citation);
          }}
        />
      ) : (
        <NotGroundedNote>{message.text}</NotGroundedNote>
      )}

      {message.citations.length > 0 ? (
        <footer className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-rule pt-3">
          <span className="text-[13px] font-semibold text-ink-3">
            {plural(message.citations.length, "source")}
          </span>
          {message.citations.map((citation) => {
            const origin = ORIGIN[citation.origin];
            const active = activeMarker === citation.marker;
            return (
              <button
                key={citation.marker}
                type="button"
                onClick={() => onInspect(citation)}
                aria-pressed={active}
                className={`flex max-w-[18rem] items-center gap-1.5 rounded-full border px-2 py-0.5 text-[13px] transition-colors ${
                  active ? "border-ink-3 bg-sunken text-ink" : "border-rule text-ink-2 hover:bg-sunken"
                }`}
              >
                <span
                  aria-hidden
                  className={`flex size-4 shrink-0 items-center justify-center rounded-full text-[10px] font-bold text-ink ${origin.mark}`}
                >
                  {citation.marker}
                </span>
                <span className="truncate">{citation.title}</span>
              </button>
            );
          })}
        </footer>
      ) : null}

      {topicName && message.grounded ? (
        <Link
          href={`/student/practice?topic=${message.topicIds[0]}`}
          className={buttonClass("secondary", "sm", "mt-3")}
        >
          <ListChecks className="size-4" aria-hidden />
          Practise {topicName}
        </Link>
      ) : null}
    </article>
  );
}

export default function AskPage() {
  const course = useCourse();
  const documents = useQuery(`documents:${course.id}`, () => api.listDocuments(course.id));
  const topics = useQuery(`topics:${course.id}`, () => api.listTopics(course.id));

  // null means "everything that is ready", so the default needs no effect.
  const [picked, setPicked] = useState<Set<string> | null>(null);
  const [useWeb, setUseWeb] = useState(false);
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [inspected, setInspected] = useState<Citation | null>(null);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const nextId = useRef(0);
  const endRef = useRef<HTMLDivElement>(null);

  const all = useMemo(() => documents.data ?? [], [documents.data]);
  const ready = useMemo(() => all.filter((d) => d.status === "ready"), [all]);
  const selected = useMemo(
    () => picked ?? new Set(ready.map((d) => d.id)),
    [picked, ready],
  );

  // The scope the backend understands is derived from the visible choices.
  const scope: Scope = useWeb
    ? "class_plus_web"
    : ready.some((d) => d.origin !== "class" && selected.has(d.id))
      ? "class_plus_external"
      : "class_only";

  const topicName = (message: ChatMessage) => {
    const id = message.topicIds[0];
    return topics.data?.find((t) => t.id === id)?.name ?? null;
  };

  function inspect(citation: Citation) {
    setInspected(citation);
    setDetailOpen(true); // only visible below xl
  }

  async function ask(text: string) {
    const trimmed = text.trim();
    if (!trimmed || asking) return;
    if (selected.size === 0 && !useWeb) {
      setError(new Error("Pick at least one source, or turn on web search."));
      return;
    }

    setAsking(true);
    setError(null);
    setQuestion("");
    setStreaming("");
    nextId.current += 1;
    setMessages((list) => [
      ...list,
      {
        id: `q-${nextId.current}`,
        role: "user",
        text: trimmed,
        citations: [],
        grounded: true,
        usedWeb: false,
        scope,
        topicIds: [],
        createdAt: new Date().toISOString(),
      },
    ]);
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });

    try {
      const answer = await api.ask(course.id, trimmed, scope, setStreaming, [...selected]);
      setMessages((list) => [...list, answer]);
      if (answer.citations.length > 0) setInspected(answer.citations[0]);
    } catch (reason) {
      setError(reason);
    } finally {
      setStreaming(null);
      setAsking(false);
      window.setTimeout(() => endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }), 50);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void ask(question);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void ask(question);
  }

  const rail = (
    <SourceRail
      documents={all}
      selected={selected}
      onChange={setPicked}
      useWeb={useWeb}
      onUseWebChange={setUseWeb}
    />
  );

  return (
    <div className="xl:grid xl:h-[calc(100dvh-5rem)] xl:grid-cols-[14rem_minmax(0,1fr)_17rem] xl:gap-5 xl:overflow-hidden">
      {/* Sources: a pane on wide screens, a dialog below that */}
      <aside className="hidden min-h-0 border-r border-rule pr-4 xl:block">{rail}</aside>

      <div className="flex min-h-0 flex-col xl:overflow-hidden">
        <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-xl font-bold tracking-[-0.01em] text-ink">Ask {course.code}</h1>
            <p className="text-sm text-ink-2">
              Every sentence is highlighted in the colour of the source it came from.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setSourcesOpen(true)}
            className={buttonClass("secondary", "sm", "xl:hidden")}
          >
            <PanelLeft className="size-4" aria-hidden />
            <SourceSummary documents={all} selected={selected} useWeb={useWeb} />
          </button>
        </header>

        {/* max-w caps the line length below xl, where the column runs wide;
            at xl the pane is already narrow, so it never binds. */}
        <div className="mx-auto min-h-0 w-full max-w-3xl flex-1 space-y-4 xl:max-w-none xl:overflow-y-auto xl:pr-1">
          {messages.length === 0 && streaming === null ? (
            <div className="rounded-lg border border-dashed border-rule-strong p-6">
              <p className="font-semibold text-ink">
                Ask anything from {course.name}
              </p>
              <p className="mt-1 text-sm text-ink-2">
                Answers are built only from the sources ticked beside this conversation.
              </p>
              <ul className="mt-3 flex flex-wrap gap-2">
                {SUGGESTED_QUESTIONS.map((suggestion) => (
                  <li key={suggestion}>
                    <button
                      type="button"
                      onClick={() => void ask(suggestion)}
                      className="rounded-full border border-rule-strong bg-surface px-3 py-1.5 text-left text-sm text-ink hover:bg-sunken"
                    >
                      {suggestion}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {messages.map((message) =>
            message.role === "user" ? (
              <p
                key={message.id}
                className="ml-auto max-w-[36rem] rounded-lg bg-action px-4 py-2.5 font-semibold text-action-ink"
              >
                {message.text}
              </p>
            ) : (
              <Answer
                key={message.id}
                message={message}
                onInspect={inspect}
                activeMarker={inspected?.marker ?? null}
                topicName={topicName(message)}
              />
            ),
          )}

          {streaming !== null ? (
            <div className="rounded-lg border border-rule bg-surface p-5">
              {streaming === "" ? (
                <p className="flex items-center gap-2 text-ink-2">
                  <LoaderCircle className="size-4 animate-spin" aria-hidden />
                  Searching {plural(selected.size, "source")}
                  {useWeb ? " and the web" : ""}
                </p>
              ) : (
                <p className="reading text-ink">{streaming}</p>
              )}
            </div>
          ) : null}

          {error ? <ErrorNote error={error} /> : null}
          {documents.error ? <ErrorNote error={documents.error} onRetry={documents.reload} /> : null}
          <div ref={endRef} />
        </div>

        <form
          onSubmit={onSubmit}
          className="sticky bottom-0 mx-auto mt-4 w-full max-w-3xl shrink-0 border-t border-rule bg-page pt-3 pb-4 xl:static xl:max-w-none xl:pb-0"
        >
          <div className="flex items-end gap-2">
            <Textarea
              id="question"
              rows={2}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder={`Ask about ${course.name}`}
              aria-label="Your question"
              className="flex-1 resize-none"
            />
            <Button
              type="submit"
              variant="primary"
              busy={asking}
              icon={<Send className="size-4" aria-hidden />}
            >
              <span className="sr-only sm:not-sr-only">Ask</span>
            </Button>
          </div>
          <p className="mt-1.5 text-[13px] text-ink-3">
            Enter sends, Shift and Enter start a new line. Answers can be wrong, so check the sources.
          </p>
        </form>
      </div>

      {/* Inspector: a pane on wide screens, a dialog below that */}
      <aside className="hidden min-h-0 border-l border-rule pl-4 xl:block">
        <Inspector citation={inspected} />
      </aside>

      <Dialog open={sourcesOpen} onClose={() => setSourcesOpen(false)} title="Sources">
        <div className="max-h-[60vh]">{rail}</div>
      </Dialog>

      <Dialog
        open={detailOpen && inspected !== null}
        onClose={() => setDetailOpen(false)}
        title="Source"
      >
        {inspected ? <CitationDetail citation={inspected} /> : null}
      </Dialog>
    </div>
  );
}
