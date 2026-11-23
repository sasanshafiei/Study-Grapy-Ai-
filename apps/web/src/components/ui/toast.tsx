"use client";

import { CircleAlert, CircleCheck } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

interface Toast {
  id: number;
  message: string;
  kind: "done" | "error";
}

type Notify = (message: string, kind?: Toast["kind"]) => void;

const ToastContext = createContext<Notify>(() => {});

/** Short confirmations ("Saved", "Removed") that appear and leave on their own. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const notify = useCallback<Notify>((message, kind = "done") => {
    const id = nextId.current;
    nextId.current += 1;
    setToasts((list) => [...list.slice(-2), { id, message, kind }]);
    window.setTimeout(
      () => setToasts((list) => list.filter((t) => t.id !== id)),
      kind === "error" ? 7000 : 4000,
    );
  }, []);

  const value = useMemo(() => notify, [notify]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        aria-live="polite"
        className="no-print pointer-events-none fixed inset-x-0 bottom-4 z-50 flex flex-col items-center gap-2 px-4"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            role={t.kind === "error" ? "alert" : "status"}
            className="pointer-events-auto flex max-w-md items-center gap-2 rounded-md bg-action px-4 py-2.5 text-sm font-semibold text-action-ink shadow-lg"
          >
            {t.kind === "error" ? (
              <CircleAlert className="size-4 shrink-0" aria-hidden />
            ) : (
              <CircleCheck className="size-4 shrink-0" aria-hidden />
            )}
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): Notify {
  return useContext(ToastContext);
}
