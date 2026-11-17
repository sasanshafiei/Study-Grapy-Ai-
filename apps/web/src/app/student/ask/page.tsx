"use client";

import { Globe, LoaderCircle, Send } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api, type ChatMessage, type Scope } from "@/lib/api";
import { SUGGESTED_QUESTIONS } from "@/lib/api/mock-data";
import { errorMessage, plural } from "@/lib/format";
import { AnswerText, NotGroundedNote, OriginKey, SourceCard } from "@/components/answer";
import { useCourse } from "@/components/shell/app-shell";
import { Button } from "@/components/ui/button";
import { ErrorNote } from "@/components/ui/feedback";
import { PageHeader } from "@/components/ui/page";
import { Textarea } from "@/components/ui/field";

const SCOPES: { value: Scope; label: string; help: string }[] = [
  {
    value: "class_only",
    label: "Class only",
    help: "Answers come from this course's uploaded materials and nothing else.",
  },
  {
    value: "class_plus_external",
    label: "Class and sources",
    help: "Adds the open sources StudyGraph fetched for this course's topics.",
  },
  {
    value: "class_plus_web",
    label: "Add web search",
    help: "Also searches the web live. Slower, and the results are not reviewed by your professor.",
  },
];

function ScopePicker({ value, onChange }: { value: Scope; onChange: (next: Scope) => void }) {
  const help = SCOPES.find((s) => s.value === value)?.help;
  return (
    <div>
      <div role="group" aria-label="Where answers may come from" className="flex flex-wrap gap-1 rounded-md bg-sunken p-1">
        {SCOPES.map((scope) => {
          const active = scope.value === value;
          return (
            <button
              key={scope.value}
              type="button"
              aria-pressed={active}
              onClick={() => onChange(scope.value)}
              className={`flex items-center gap-1.5 rounded px-3 py-1.5 text-sm font-semibold transition-colors ${
                active ? "bg-surface text-ink shadow-sm" : "text-ink-2 hover:text-ink"
              }`}
            >
              {scope.value === "class_plus_web" ? <Globe className="size-3.5" aria-hidden /> : null}
              {scope.label}
            </button>
          );
        })}
      </div>
      <p className="mt-1.5 text-sm text-ink-3">{help}</p>
    </div>
  );
}

function Answer({ message }: { message: ChatMessage }) {
  const scrollTo = (marker: number) => {
    document.getElementById(`source-${message.id}-${marker}`)?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
  };

  return (
    <div className="rounded-lg border border-rule bg-surface p-5">
      {message.grounded ? (
        <AnswerText text={message.text} citations={message.citations} onCitationClick={scrollTo} />
      ) : (
        <NotGroundedNote>{message.text}</NotGroundedNote>
      )}

      {message.citations.length > 0 ? (
        <div className="mt-5 border-t border-rule pt-4">
          <h3 className="text-sm font-semibold text-ink">
            {plural(message.citations.length, "source")}
          </h3>
          <ul className="mt-2 space-y-2">
            {message.citations.map((citation) => (
              <SourceCard
                key={citation.marker}
                citation={citation}
                id={`source-${message.id}-${citation.marker}`}
              />
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

export default function AskPage() {
  const course = useCourse();
  const [scope, setScope] = useState<Scope>("class_plus_external");
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const nextId = useRef(0);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, streaming]);

  async function ask(text: string) {
    const trimmed = text.trim();
    if (!trimmed || asking) return;

    setAsking(true);
    setError(null);
    setQuestion("");
    setStreaming("");
    nextId.current += 1;
    const asked: ChatMessage = {
      id: `q-${nextId.current}`,
      role: "user",
      text: trimmed,
      citations: [],
      grounded: true,
      usedWeb: false,
      scope,
      createdAt: new Date().toISOString(),
    };
    setMessages((list) => [...list, asked]);

    try {
      const answer = await api.ask(course.id, trimmed, scope, setStreaming);
      setMessages((list) => [...list, answer]);
    } catch (reason) {
      setError(reason);
    } finally {
      setStreaming(null);
      setAsking(false);
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

  return (
    <div className="flex min-h-[calc(100dvh-7rem)] flex-col">
      <PageHeader
        title="Ask"
        description="Answers are built from this course's materials. Every sentence is highlighted in the colour of the source it came from."
      />

      <OriginKey className="mb-5" />

      {/* grow + shrink-0 keeps the thread at its full height, so long answers
          are never clipped behind the composer below. */}
      <div className="grow shrink-0 space-y-5">
        {messages.length === 0 && !streaming ? (
          <div className="rounded-lg border border-dashed border-rule-strong p-6">
            <p className="font-semibold text-ink">Ask anything from {course.code}</p>
            <p className="mt-1 text-ink-2">Or start with one of these:</p>
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
            <p key={message.id} className="ml-auto max-w-[40rem] rounded-lg bg-action px-4 py-2.5 font-semibold text-action-ink">
              {message.text}
            </p>
          ) : (
            <Answer key={message.id} message={message} />
          ),
        )}

        {streaming !== null ? (
          <div className="rounded-lg border border-rule bg-surface p-5">
            {streaming === "" ? (
              <p className="flex items-center gap-2 text-ink-2">
                <LoaderCircle className="size-4 animate-spin" aria-hidden />
                Searching this course&rsquo;s materials
              </p>
            ) : (
              <p className="reading text-ink">{streaming}</p>
            )}
          </div>
        ) : null}

        {error ? <ErrorNote error={error} /> : null}
        <div ref={endRef} />
      </div>

      <form onSubmit={onSubmit} className="sticky bottom-0 mt-6 border-t border-rule bg-page pt-4 pb-6">
        <ScopePicker value={scope} onChange={setScope} />
        <div className="mt-3 flex items-end gap-2">
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
          <Button type="submit" variant="primary" busy={asking} icon={<Send className="size-4" aria-hidden />}>
            <span className="sr-only sm:not-sr-only">Ask</span>
          </Button>
        </div>
        <p className="mt-1.5 text-sm text-ink-3">
          Enter sends. Shift and Enter start a new line. Answers can be wrong, so check the sources.
        </p>
        {error ? <p className="sr-only">{errorMessage(error)}</p> : null}
      </form>
    </div>
  );
}
