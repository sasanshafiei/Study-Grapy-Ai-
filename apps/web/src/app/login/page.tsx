"use client";

import { ChevronRight } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "@/lib/api";
import { DEMO_PASSWORD } from "@/lib/api/mock";
import { HOME, setSession, useSession } from "@/lib/session";
import { errorMessage } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/field";
import { Wordmark } from "@/components/shell/wordmark";
import { ORIGIN } from "@/components/ui/status";

const DEMO_ACCOUNTS = [
  { role: "Student", email: "student@studygraph.test", what: "Ask, practise, track progress" },
  { role: "Professor", email: "professor@studygraph.test", what: "Build and approve exams" },
  { role: "Admin", email: "admin@studygraph.test", what: "Manage users and sources" },
];

/** The sample answer on the sign-in panel: what the product does, in one look. */
const SAMPLE = [
  {
    origin: "class" as const,
    text: "The limiting reactant is used up first, so it sets how much product can form.",
    source: "Lecture 04 - Stoichiometry.pdf, slide 12",
  },
  {
    origin: "external" as const,
    text: "Work out how much product each reactant could make; the one that makes less is limiting.",
    source: "OpenStax Chemistry 2e, 4.4",
  },
  {
    origin: "web" as const,
    text: "Industrial plants pick conditions that trade yield against reaction rate.",
    source: "Haber process, fetched today",
  },
];

export default function LoginPage() {
  const session = useSession();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (session) router.replace(HOME[session.user.role]);
  }, [session, router]);

  async function signIn(withEmail: string, withPassword: string) {
    setBusy(true);
    setError(null);
    try {
      const next = await api.login(withEmail, withPassword);
      setSession(next);
      router.replace(HOME[next.user.role]);
    } catch (reason) {
      setError(
        reason instanceof ApiError && reason.status === 0
          ? `${errorMessage(reason)} You can also run the app in mock mode to try it without a server.`
          : errorMessage(reason),
      );
      setBusy(false);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!email.trim() || !password) {
      setError("Enter your email and password.");
      return;
    }
    void signIn(email, password);
  }

  return (
    <div className="grid min-h-dvh lg:grid-cols-[1.1fr_1fr]">
      {/* What the product does, shown rather than described. */}
      <section className="graph-paper relative order-last flex flex-col justify-center px-6 py-12 text-cover-ink sm:px-10 lg:order-first lg:px-14">
        <div className="mx-auto w-full max-w-xl">
          <h2 className="text-[26px] leading-[1.25] font-bold tracking-[-0.01em] sm:text-[32px]">
            Every sentence shows where it came from.
          </h2>
          <p className="mt-3 max-w-prose text-cover-muted">
            StudyGraph answers from your course materials and from open sources it fetches on its own.
            The colour behind a sentence tells you which.
          </p>

          <div className="mt-8 rounded-lg bg-surface p-5 shadow-2xl">
            <p className="text-sm font-semibold text-ink-3">How do I find the limiting reactant?</p>
            <p className="reading mt-3 text-ink">
              {SAMPLE.map((part, i) => (
                <span key={part.origin}>
                  <mark className={`hl ${ORIGIN[part.origin].highlight}`}>{part.text}</mark>
                  <span className="ml-0.5 align-super text-[0.7em] font-bold text-ink-2">{i + 1}</span>{" "}
                </span>
              ))}
            </p>
            <ul className="mt-4 space-y-2 border-t border-rule pt-3">
              {SAMPLE.map((part, i) => (
                <li key={part.origin} className="flex items-start gap-2 text-sm">
                  <span
                    aria-hidden
                    className={`mt-px flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold text-ink ${ORIGIN[part.origin].mark}`}
                  >
                    {i + 1}
                  </span>
                  <span className="min-w-0 text-ink-2">
                    <span className="font-semibold text-ink">{ORIGIN[part.origin].label}.</span>{" "}
                    {part.source}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      {/* Sign in */}
      <section className="flex flex-col justify-center bg-page px-6 py-12 sm:px-10 lg:px-14">
        <div className="mx-auto w-full max-w-sm">
          <Wordmark className="text-ink" />
          <h1 className="mt-8 text-2xl font-bold tracking-[-0.01em] text-ink">Sign in</h1>
          <p className="mt-1.5 text-ink-2">Use your university account.</p>

          <form onSubmit={onSubmit} className="mt-6 space-y-4" noValidate>
            <Field label="Email" htmlFor="email">
              <Input
                id="email"
                name="email"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@university.edu"
                required
              />
            </Field>
            <Field label="Password" htmlFor="password">
              <Input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </Field>

            {error ? (
              <p role="alert" className="rounded-md bg-crit-wash px-3 py-2 text-sm text-crit-ink">
                {error}
              </p>
            ) : null}

            <Button type="submit" variant="primary" busy={busy} className="w-full">
              Sign in
            </Button>
          </form>

          {api.mode === "mock" ? (
            <div className="mt-8 rounded-lg border border-rule bg-surface p-4">
              <p className="text-sm font-semibold text-ink">Try a role</p>
              <p className="mt-0.5 text-sm text-ink-2">
                Sample data only. Nothing is sent to a server.
              </p>
              <ul className="mt-3 space-y-1.5">
                {DEMO_ACCOUNTS.map((account) => (
                  <li key={account.email}>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void signIn(account.email, DEMO_PASSWORD)}
                      className="flex w-full items-center gap-3 rounded-md px-2 py-1.5 text-left hover:bg-sunken disabled:opacity-50"
                    >
                      <span className="font-semibold text-ink">{account.role}</span>
                      <span className="ml-auto text-sm text-ink-3">{account.what}</span>
                      <ChevronRight className="size-4 shrink-0 text-ink-3" aria-hidden />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      </section>
    </div>
  );
}
