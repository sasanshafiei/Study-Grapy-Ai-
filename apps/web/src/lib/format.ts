/** Small formatting helpers. All output is plain text for the UI. */

const DAY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
const FULL = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });
const TIME = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
const WEEKDAY = new Intl.DateTimeFormat("en-GB", { weekday: "short" });

export const percent = (n: number) => `${Math.round(n * 100)}%`;

export function shortDate(iso: string): string {
  return DAY.format(new Date(iso));
}

export function fullDate(iso: string): string {
  return FULL.format(new Date(iso));
}

export function weekday(iso: string): string {
  return WEEKDAY.format(new Date(iso));
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** "Today", "Yesterday", "3 days ago", "In 2 days", or a date further out. */
export function relativeDay(iso: string): string {
  const then = new Date(iso);
  const days = Math.round((startOfDay(then) - startOfDay(new Date())) / 86_400_000);
  if (days === 0) return "Today";
  if (days === -1) return "Yesterday";
  if (days === 1) return "Tomorrow";
  if (days < 0 && days >= -13) return `${-days} days ago`;
  if (days > 0 && days <= 13) return `In ${days} days`;
  return fullDate(iso);
}

/** "Today, 14:05" or "12 Nov, 03:00". */
export function dateTime(iso: string): string {
  const d = new Date(iso);
  const day = relativeDay(iso);
  const label = day === "Today" || day === "Yesterday" ? day : shortDate(iso);
  return `${label}, ${TIME.format(d)}`;
}

/** 1,284 below ten thousand, then 12.9K, 4.2M. */
export function compact(n: number): string {
  if (Math.abs(n) < 10_000) return n.toLocaleString("en-GB");
  if (Math.abs(n) < 1_000_000) return `${(n / 1000).toFixed(n < 100_000 ? 1 : 0).replace(/\.0$/, "")}K`;
  return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function minutes(n: number): string {
  if (n < 60) return `${n} min`;
  const h = Math.floor(n / 60);
  const m = n % 60;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Try again.";
}
