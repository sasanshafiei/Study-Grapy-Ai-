"use client";

import { useSyncExternalStore } from "react";
import type { Role, Session } from "@/lib/api/types";

/**
 * The signed-in session, kept in localStorage so it survives a reload.
 *
 * `useSession()` returns:
 *   undefined  while the page is still hydrating (we do not know yet)
 *   null       when nobody is signed in
 *   Session    when someone is
 *
 * Note for later: localStorage is fine for a capstone demo. For a public
 * deployment, move the refresh token into an httpOnly cookie set by core-api.
 */

const KEY = "sg-session";

let cached: Session | null | undefined;
const listeners = new Set<() => void>();

function read(): Session | null {
  if (cached !== undefined) return cached;
  try {
    const raw = window.localStorage.getItem(KEY);
    cached = raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    cached = null;
  }
  return cached;
}

export function getSession(): Session | null {
  return typeof window === "undefined" ? null : read();
}

export function setSession(session: Session | null) {
  cached = session;
  try {
    if (session) window.localStorage.setItem(KEY, JSON.stringify(session));
    else window.localStorage.removeItem(KEY);
  } catch {
    // Storage unavailable: the session lasts for this page load only.
  }
  listeners.forEach((notify) => notify());
}

function subscribe(notify: () => void) {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

export function useSession(): Session | null | undefined {
  return useSyncExternalStore(subscribe, read, () => undefined);
}

export const HOME: Record<Role, string> = {
  student: "/student",
  professor: "/professor",
  admin: "/admin",
};
