"use client";

import {
  Activity,
  ClipboardList,
  Database,
  FileText,
  ListChecks,
  LogOut,
  Menu,
  MessageSquareText,
  RotateCcw,
  Target,
  Users,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  Suspense,
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { api, type Course, type Role } from "@/lib/api";
import { resetMockData } from "@/lib/api/mock";
import { HOME, setSession, useSession } from "@/lib/session";
import { useQuery } from "@/lib/use-query";
import { ErrorNote, LoadingLines } from "@/components/ui/feedback";
import { ThemeToggle } from "./theme-toggle";
import { Wordmark } from "./wordmark";

interface NavItem {
  href: string;
  label: string;
  Icon: typeof Users;
  /** Match the path exactly (for a section's home page). */
  exact?: boolean;
}

const NAV: Record<Role, NavItem[]> = {
  student: [
    { href: "/student", label: "Today", Icon: Target, exact: true },
    { href: "/student/ask", label: "Ask", Icon: MessageSquareText },
    { href: "/student/practice", label: "Practice", Icon: ListChecks },
    { href: "/student/materials", label: "Materials", Icon: FileText },
  ],
  professor: [
    { href: "/professor", label: "Class", Icon: Users, exact: true },
    { href: "/professor/exams", label: "Exams", Icon: ClipboardList },
    { href: "/professor/materials", label: "Materials", Icon: FileText },
  ],
  admin: [
    { href: "/admin", label: "Overview", Icon: Activity, exact: true },
    { href: "/admin/users", label: "Users", Icon: Users },
    { href: "/admin/sources", label: "Sources", Icon: Database },
  ],
};

const ROLE_LABEL: Record<Role, string> = {
  student: "Student",
  professor: "Professor",
  admin: "Admin",
};

const CourseContext = createContext<Course | null>(null);

/** The course the signed-in student or professor is working in. */
export function useCourse(): Course {
  const course = useContext(CourseContext);
  if (!course) throw new Error("useCourse() needs a course. Admin screens do not have one.");
  return course;
}

function NavList({
  items,
  pathname,
  onNavigate,
}: {
  items: NavItem[];
  pathname: string | null;
  onNavigate?: () => void;
}) {
  return (
    <ul className="space-y-0.5">
      {items.map(({ href, label, Icon, exact }) => {
        const active =
          pathname !== null && (exact ? pathname === href : pathname === href || pathname.startsWith(`${href}/`));
        return (
          <li key={href}>
            <Link
              href={href}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              className={`flex items-center gap-3 rounded-md px-3 py-2 text-[15px] transition-colors ${
                active
                  ? "bg-cover-active font-semibold text-cover-ink shadow-[inset_3px_0_0_var(--cover-ink)]"
                  : "text-cover-muted hover:bg-cover-active hover:text-cover-ink"
              }`}
            >
              <Icon className="size-[18px] shrink-0" aria-hidden />
              {label}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

/** Reads the URL, so it sits behind Suspense (see the shell below). */
function ActiveNav(props: { items: NavItem[]; onNavigate?: () => void }) {
  const pathname = usePathname();
  return <NavList {...props} pathname={pathname} />;
}

function SidebarBody({
  role,
  course,
  userName,
  onNavigate,
}: {
  role: Role;
  course: Course | null;
  userName: string;
  onNavigate?: () => void;
}) {
  const quiet =
    "flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-cover-muted hover:bg-cover-active hover:text-cover-ink";

  return (
    <div className="flex h-full flex-col">
      {course ? (
        <div className="mb-5 border-b border-white/10 px-3 pb-4">
          <div className="text-sm text-cover-muted">{course.code}</div>
          <div className="font-semibold text-cover-ink">{course.name}</div>
        </div>
      ) : null}

      <nav aria-label="Main">
        <Suspense fallback={<NavList items={NAV[role]} pathname={null} />}>
          <ActiveNav items={NAV[role]} onNavigate={onNavigate} />
        </Suspense>
      </nav>

      <div className="mt-auto space-y-0.5 pt-6">
        {api.mode === "mock" ? (
          <div className="mb-2 rounded-md border border-white/10 px-3 py-2.5 text-sm text-cover-muted">
            <p>
              <span className="font-semibold text-cover-ink">Sample data.</span> Nothing here is sent to a
              server.
            </p>
            <button
              type="button"
              className="mt-1.5 inline-flex items-center gap-1.5 font-semibold text-cover-ink underline underline-offset-2"
              onClick={() => {
                resetMockData();
                window.location.reload();
              }}
            >
              <RotateCcw className="size-3.5" aria-hidden />
              Reset sample data
            </button>
          </div>
        ) : null}
        <ThemeToggle className={quiet} />
        <button type="button" className={quiet} onClick={() => setSession(null)}>
          <LogOut className="size-4" aria-hidden />
          Sign out
        </button>
        <div className="border-t border-white/10 px-3 pt-3 text-sm">
          <div className="truncate font-semibold text-cover-ink">{userName}</div>
          <div className="text-cover-muted">{ROLE_LABEL[role]}</div>
        </div>
      </div>
    </div>
  );
}

function FullPageNote({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-h-dvh place-items-center px-6">
      <div className="text-center">
        <Wordmark className="text-ink" />
        <div className="mt-3 text-ink-2">{children}</div>
      </div>
    </div>
  );
}

/**
 * The frame around every signed-in screen: navigation, the current course,
 * and the check that this account is allowed in this section.
 *
 * The check here is for navigation only. The server must enforce roles on
 * every request; hiding a link is not security.
 */
export function AppShell({ role, children }: { role: Role; children: ReactNode }) {
  const session = useSession();
  const router = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    if (session === null) router.replace("/login");
    else if (session && session.user.role !== role) router.replace(HOME[session.user.role]);
  }, [session, role, router]);

  const allowed = Boolean(session) && session?.user.role === role;
  const needsCourse = role !== "admin";
  const courses = useQuery(allowed && needsCourse ? "courses" : null, () => api.listCourses());
  const course = courses.data?.[0] ?? null;

  if (!session || !allowed) {
    return <FullPageNote>{session === undefined ? "Loading" : "Taking you to the right place"}</FullPageNote>;
  }

  const body = (onNavigate?: () => void) => (
    <SidebarBody role={role} course={course} userName={session.user.displayName} onNavigate={onNavigate} />
  );

  return (
    <div className="min-h-dvh lg:pl-60">
      {/* Desktop: fixed sidebar */}
      <aside className="no-print fixed inset-y-0 left-0 hidden w-60 flex-col bg-cover px-3 py-5 lg:flex">
        <Link href={HOME[role]} className="mb-6 px-3 text-cover-ink">
          <Wordmark />
        </Link>
        {body()}
      </aside>

      {/* Small screens: top bar with a menu */}
      <header className="no-print sticky top-0 z-30 bg-cover lg:hidden">
        <div className="flex h-14 items-center justify-between px-4">
          <Link href={HOME[role]} className="text-cover-ink">
            <Wordmark />
          </Link>
          <button
            type="button"
            onClick={() => setMenuOpen((v) => !v)}
            aria-expanded={menuOpen}
            aria-controls="mobile-menu"
            className="flex items-center gap-2 rounded-md px-3 py-2 text-sm font-semibold text-cover-ink hover:bg-cover-active"
          >
            {menuOpen ? <X className="size-5" aria-hidden /> : <Menu className="size-5" aria-hidden />}
            Menu
          </button>
        </div>
        {menuOpen ? (
          <div id="mobile-menu" className="max-h-[calc(100dvh-3.5rem)] overflow-y-auto px-3 pt-2 pb-5">
            {body(() => setMenuOpen(false))}
          </div>
        ) : null}
      </header>

      <main className="mx-auto max-w-6xl px-5 py-8 sm:px-8 lg:px-10 lg:py-10">
        <CourseContext.Provider value={course}>
          {needsCourse && !course ? (
            courses.error ? (
              <ErrorNote error={courses.error} onRetry={courses.reload} />
            ) : (
              <LoadingLines label="Loading your course" />
            )
          ) : (
            <Suspense fallback={<LoadingLines />}>{children}</Suspense>
          )}
        </CourseContext.Provider>
      </main>
    </div>
  );
}
