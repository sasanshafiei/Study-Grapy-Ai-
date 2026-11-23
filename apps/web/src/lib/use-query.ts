"use client";

import { useCallback, useEffect, useEffectEvent, useState } from "react";

interface QueryState<T> {
  key: string | null;
  data: T | undefined;
  error: Error | null;
  settledRun: string | null;
}

/**
 * Load data for a screen and keep it while it refreshes.
 *
 *   const { data, error, loading, reload } = useQuery("exams", () => api.listExams(id));
 *
 * - `key` names what is being loaded. Change it and the hook loads again.
 *   Pass null to wait (for example until a course id is known).
 * - While a reload is in flight `data` keeps its previous value, so a screen
 *   can dim instead of flashing empty.
 */
export function useQuery<T>(key: string | null, fetcher: () => Promise<T>) {
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<QueryState<T>>({
    key: null,
    data: undefined,
    error: null,
    settledRun: null,
  });

  const run = `${key}#${tick}`;
  const load = useEffectEvent(fetcher);

  useEffect(() => {
    if (key === null) return;
    let alive = true;
    load().then(
      (data) => {
        if (alive) setState({ key, data, error: null, settledRun: run });
      },
      (reason: unknown) => {
        if (!alive) return;
        const error = reason instanceof Error ? reason : new Error(String(reason));
        setState((s) => ({
          key,
          data: s.key === key ? s.data : undefined,
          error,
          settledRun: run,
        }));
      },
    );
    return () => {
      alive = false;
    };
  }, [key, run]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  const setData = useCallback(
    (data: T) => setState((s) => ({ ...s, key, data, error: null })),
    [key],
  );

  const current = state.key === key;
  return {
    data: current ? state.data : undefined,
    error: current ? state.error : null,
    loading: key !== null && state.settledRun !== run,
    reload,
    setData,
  };
}

/** Call `reload` every `ms` while `active` is true (a job is still running). */
export function usePolling(active: boolean, reload: () => void, ms = 1500) {
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(reload, ms);
    return () => window.clearInterval(id);
  }, [active, reload, ms]);
}
