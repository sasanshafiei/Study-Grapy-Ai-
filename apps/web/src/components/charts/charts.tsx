"use client";

import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/page";

/*
  Charts for StudyGraph, drawn as plain SVG.

  House rules (from the team's data-viz guide):
    - one blue for data; every chart here plots a single series
    - thin marks: 2px lines, columns at most 24px wide with a 4px rounded cap
    - hairline grid, never dashed
    - every value is reachable without hovering: each chart has a table view
    - hover and keyboard focus show the same tooltip
*/

export interface ChartPoint {
  /** Short label for the axis: "12 Nov". */
  label: string;
  /** Full label for the tooltip and table: "Wed 12 Nov". */
  longLabel: string;
  value: number | null;
  /** Extra line for the tooltip and table: "9 answers". */
  detail?: string;
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));

function niceMax(value: number): number {
  if (value <= 0) return 1;
  const power = Math.pow(10, Math.floor(Math.log10(value)));
  const fraction = value / power;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  return nice * power;
}

/** Arrow keys move the reading position; Escape clears it. */
function useReadout(count: number) {
  const [active, setActive] = useState<number | null>(null);
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      const step = e.key === "ArrowLeft" ? -1 : 1;
      setActive((i) => clamp((i ?? count - 1) + step, 0, count - 1));
    } else if (e.key === "Home") {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setActive(count - 1);
    } else if (e.key === "Escape") {
      setActive(null);
    }
  };
  return { active, setActive, onKeyDown };
}

function Tooltip({
  x,
  width,
  point,
  value,
}: {
  x: number;
  width: number;
  point: ChartPoint;
  value: string;
}) {
  return (
    <div
      aria-live="polite"
      className="pointer-events-none absolute top-0 z-10 w-36 -translate-x-1/2 rounded-md border border-rule bg-surface px-2.5 py-1.5 text-center shadow-md"
      style={{ left: clamp(x, 76, Math.max(76, width - 76)) }}
    >
      <div className="text-[15px] leading-tight font-bold text-ink">{value}</div>
      <div className="text-xs text-ink-2">{point.longLabel}</div>
      {point.detail ? <div className="text-xs text-ink-3">{point.detail}</div> : null}
    </div>
  );
}

function axisLabelIndexes(count: number): number[] {
  if (count <= 1) return [0];
  if (count <= 4) return Array.from({ length: count }, (_, i) => i);
  return [0, Math.floor((count - 1) / 2), count - 1];
}

// --- Line: change over time ---------------------------------------------------

export function TrendLine({
  points,
  format,
  max = 1,
  emptyValue = "No data",
  height = 208,
  label,
}: {
  points: ChartPoint[];
  format: (value: number) => string;
  max?: number;
  emptyValue?: string;
  height?: number;
  /** Read by screen readers: what the chart shows. */
  label: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const { active, setActive, onKeyDown } = useReadout(points.length);

  const M = { top: 34, right: 52, bottom: 26, left: 44 };
  const plotW = Math.max(0, width - M.left - M.right);
  const plotH = height - M.top - M.bottom;
  const count = points.length;
  const x = (i: number) => M.left + (count > 1 ? (i / (count - 1)) * plotW : plotW / 2);
  const y = (v: number) => M.top + plotH * (1 - v / max);
  const baseline = M.top + plotH;

  // Split the series wherever a day has no value, so the line shows a gap.
  const runs: { i: number; v: number }[][] = [];
  let run: { i: number; v: number }[] = [];
  points.forEach((p, i) => {
    if (p.value === null) {
      if (run.length) runs.push(run);
      run = [];
    } else {
      run.push({ i, v: p.value });
    }
  });
  if (run.length) runs.push(run);

  const lastRun = runs.at(-1);
  const last = lastRun?.at(-1);
  const ticks = [0, max / 2, max];
  const activePoint = active === null ? null : points[active];

  return (
    <div
      ref={ref}
      className="relative rounded"
      tabIndex={0}
      role="group"
      aria-label={`${label}. Use the left and right arrow keys to read each value.`}
      onKeyDown={onKeyDown}
      onBlur={() => setActive(null)}
    >
      {width > 0 ? (
        <svg
          width={width}
          height={height}
          className="block touch-none"
          onPointerMove={(e) => {
            const box = e.currentTarget.getBoundingClientRect();
            const ratio = plotW > 0 ? (e.clientX - box.left - M.left) / plotW : 0;
            setActive(clamp(Math.round(ratio * (count - 1)), 0, count - 1));
          }}
          onPointerLeave={() => setActive(null)}
          aria-hidden
        >
          {ticks.map((t) => (
            <g key={t}>
              <line x1={M.left} x2={M.left + plotW} y1={y(t)} y2={y(t)} stroke={t === 0 ? "var(--axis)" : "var(--grid)"} />
              <text x={M.left - 8} y={y(t) + 4} textAnchor="end" fontSize="12" fill="var(--ink-3)" style={{ fontVariantNumeric: "tabular-nums" }}>
                {format(t)}
              </text>
            </g>
          ))}

          {axisLabelIndexes(count).map((i) => (
            <text
              key={i}
              x={x(i)}
              y={height - 6}
              textAnchor={i === 0 ? "start" : i === count - 1 ? "end" : "middle"}
              fontSize="12"
              fill="var(--ink-3)"
            >
              {points[i].label}
            </text>
          ))}

          {runs.map((r) => {
            const line = r.map((p, k) => `${k === 0 ? "M" : "L"}${x(p.i)} ${y(p.v)}`).join(" ");
            const first = r[0];
            const end = r[r.length - 1];
            return (
              <g key={first.i}>
                {r.length > 1 ? (
                  <path d={`${line} L${x(end.i)} ${baseline} L${x(first.i)} ${baseline} Z`} fill="var(--series)" fillOpacity="0.1" />
                ) : null}
                {r.length > 1 ? (
                  <path d={line} fill="none" stroke="var(--series)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
                ) : (
                  <circle cx={x(first.i)} cy={y(first.v)} r="4" fill="var(--series)" stroke="var(--surface)" strokeWidth="2" />
                )}
              </g>
            );
          })}

          {last ? (
            <g>
              <circle cx={x(last.i)} cy={y(last.v)} r="4.5" fill="var(--series)" stroke="var(--surface)" strokeWidth="2" />
              {active === null ? (
                <text x={x(last.i) + 9} y={y(last.v) + 4} fontSize="13" fontWeight="700" fill="var(--ink)">
                  {format(last.v)}
                </text>
              ) : null}
            </g>
          ) : null}

          {active !== null ? (
            <g>
              <line x1={x(active)} x2={x(active)} y1={M.top} y2={baseline} stroke="var(--ink-3)" />
              {activePoint && activePoint.value !== null ? (
                <circle cx={x(active)} cy={y(activePoint.value)} r="5" fill="var(--series)" stroke="var(--surface)" strokeWidth="2" />
              ) : null}
            </g>
          ) : null}
        </svg>
      ) : (
        <div style={{ height }} />
      )}

      {active !== null && activePoint ? (
        <Tooltip
          x={x(active)}
          width={width}
          point={activePoint}
          value={activePoint.value === null ? emptyValue : format(activePoint.value)}
        />
      ) : null}
    </div>
  );
}

// --- Columns: amount per day --------------------------------------------------

export function Columns({
  points,
  format,
  height = 208,
  label,
}: {
  points: ChartPoint[];
  format: (value: number) => string;
  height?: number;
  label: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const { active, setActive, onKeyDown } = useReadout(points.length);

  const M = { top: 34, right: 8, bottom: 26, left: 44 };
  const plotW = Math.max(0, width - M.left - M.right);
  const plotH = height - M.top - M.bottom;
  const count = points.length;
  const values = points.map((p) => p.value ?? 0);
  const top = niceMax(Math.max(...values, 0));
  const band = count > 0 ? plotW / count : 0;
  const barW = clamp(band - 2, 2, 24);
  const baseline = M.top + plotH;
  const cx = (i: number) => M.left + band * (i + 0.5);
  const y = (v: number) => M.top + plotH * (1 - v / top);

  const peak = values.indexOf(Math.max(...values));
  const labelled = new Set([peak, count - 1]);
  const activePoint = active === null ? null : points[active];

  return (
    <div
      ref={ref}
      className="relative rounded"
      tabIndex={0}
      role="group"
      aria-label={`${label}. Use the left and right arrow keys to read each value.`}
      onKeyDown={onKeyDown}
      onBlur={() => setActive(null)}
    >
      {width > 0 ? (
        <svg width={width} height={height} className="block" onPointerLeave={() => setActive(null)} aria-hidden>
          {[0, top / 2, top].map((t) => (
            <g key={t}>
              <line x1={M.left} x2={M.left + plotW} y1={y(t)} y2={y(t)} stroke={t === 0 ? "var(--axis)" : "var(--grid)"} />
              <text x={M.left - 8} y={y(t) + 4} textAnchor="end" fontSize="12" fill="var(--ink-3)" style={{ fontVariantNumeric: "tabular-nums" }}>
                {format(t)}
              </text>
            </g>
          ))}

          {axisLabelIndexes(count).map((i) => (
            <text key={i} x={cx(i)} y={height - 6} textAnchor="middle" fontSize="12" fill="var(--ink-3)">
              {points[i].label}
            </text>
          ))}

          {points.map((p, i) => {
            const v = p.value ?? 0;
            const h = baseline - y(v);
            const r = Math.min(4, h, barW / 2);
            const left = cx(i) - barW / 2;
            const right = left + barW;
            const capY = y(v);
            return (
              <g key={i}>
                {h > 0 ? (
                  <path
                    d={`M${left} ${baseline} V${capY + r} Q${left} ${capY} ${left + r} ${capY} H${right - r} Q${right} ${capY} ${right} ${capY + r} V${baseline} Z`}
                    fill="var(--series)"
                    fillOpacity={active === null || active === i ? 1 : 0.45}
                  />
                ) : null}
                {labelled.has(i) && v > 0 && active === null && band >= 30 ? (
                  <text x={cx(i)} y={capY - 6} textAnchor="middle" fontSize="12" fontWeight="700" fill="var(--ink)">
                    {format(v)}
                  </text>
                ) : null}
                {/* The whole band is the hover target, not just the painted column. */}
                <rect x={M.left + band * i} y={M.top} width={band} height={plotH} fill="transparent" onPointerEnter={() => setActive(i)} />
              </g>
            );
          })}
        </svg>
      ) : (
        <div style={{ height }} />
      )}

      {active !== null && activePoint ? (
        <Tooltip x={cx(active)} width={width} point={activePoint} value={format(activePoint.value ?? 0)} />
      ) : null}
    </div>
  );
}

// --- A chart inside a panel, with its table twin -------------------------------

export function ChartPanel({
  title,
  description,
  points,
  valueHeading,
  format,
  emptyValue = "No data",
  children,
}: {
  title: string;
  description?: string;
  points: ChartPoint[];
  /** Column heading for the values in the table view: "Accuracy". */
  valueHeading: string;
  format: (value: number) => string;
  emptyValue?: string;
  children: ReactNode;
}) {
  const [asTable, setAsTable] = useState(false);
  return (
    <Panel
      title={title}
      description={description}
      actions={
        <Button size="sm" variant="quiet" onClick={() => setAsTable((v) => !v)} aria-pressed={asTable}>
          {asTable ? "Show chart" : "Show table"}
        </Button>
      }
    >
      {asTable ? (
        <div className="max-h-64 overflow-auto">
          <table className="table-grid w-full text-sm">
            <caption className="sr-only">{title}</caption>
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col" className="num">
                  {valueHeading}
                </th>
                {points.some((p) => p.detail) ? <th scope="col">Detail</th> : null}
              </tr>
            </thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.longLabel}>
                  <td>{p.longLabel}</td>
                  <td className="num">{p.value === null ? emptyValue : format(p.value)}</td>
                  {points.some((q) => q.detail) ? <td className="text-ink-2">{p.detail ?? ""}</td> : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        children
      )}
    </Panel>
  );
}

// --- Meter list: one ratio per named row ---------------------------------------

export interface MeterRow {
  id: string;
  name: string;
  /** 0 to 1. */
  value: number;
  note?: string;
  flag?: ReactNode;
}

export function MeterList({ rows, valueName }: { rows: MeterRow[]; valueName: string }) {
  return (
    <ul className="divide-y divide-rule">
      {rows.map((row) => (
        <li
          key={row.id}
          className="grid grid-cols-[1fr_auto] items-center gap-x-4 gap-y-1.5 py-3 sm:grid-cols-[minmax(0,13rem)_1fr_3rem]"
        >
          <div className="min-w-0">
            <div className="font-semibold text-ink">{row.name}</div>
            {row.flag || row.note ? (
              <div className="flex flex-wrap items-center gap-x-2 text-sm text-ink-3">
                {row.flag}
                {row.note ? <span>{row.note}</span> : null}
              </div>
            ) : null}
          </div>
          <div
            role="meter"
            aria-label={`${row.name}: ${valueName}`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(row.value * 100)}
            className="order-last col-span-2 h-2.5 overflow-hidden rounded-r-[4px] bg-series-track sm:order-none sm:col-span-1"
          >
            <div className="h-full rounded-r-[4px] bg-series" style={{ width: `${Math.round(row.value * 100)}%` }} />
          </div>
          <div className="text-right font-bold text-ink tabular-nums">{Math.round(row.value * 100)}%</div>
        </li>
      ))}
    </ul>
  );
}
