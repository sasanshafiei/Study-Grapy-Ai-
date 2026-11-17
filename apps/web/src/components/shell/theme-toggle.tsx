"use client";

import { Moon, Sun } from "lucide-react";
import { useLayoutEffect } from "react";

const KEY = "sg-theme";

function storedTheme(): "light" | "dark" | null {
  try {
    const t = window.localStorage.getItem(KEY);
    return t === "light" || t === "dark" ? t : null;
  } catch {
    return null;
  }
}

/** Switches between the light and dark themes and remembers the choice. */
export function ThemeToggle({ className = "" }: { className?: string }) {
  // In development React's Strict Mode remount clears attributes that the
  // inline script set on <html>. Re-apply the saved theme; a no-op in production.
  useLayoutEffect(() => {
    const t = storedTheme();
    if (t) document.documentElement.setAttribute("data-theme", t);
  }, []);

  function toggle() {
    const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      window.localStorage.setItem(KEY, next);
    } catch {
      // Storage unavailable: the choice lasts for this page load.
    }
  }

  return (
    <button type="button" onClick={toggle} className={className}>
      {/* Both labels are rendered; CSS shows the one for the current theme. */}
      <span className="flex items-center gap-2 dark:hidden">
        <Moon className="size-4" aria-hidden />
        Dark theme
      </span>
      <span className="hidden items-center gap-2 dark:flex">
        <Sun className="size-4" aria-hidden />
        Light theme
      </span>
    </button>
  );
}
