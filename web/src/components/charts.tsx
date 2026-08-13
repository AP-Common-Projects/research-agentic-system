import { useId, useMemo, useState } from 'react';
import { decimal } from '../lib/format';

/* --------------------------------------------------------------------------
 * NoveltyDecay — the instrument at the centre of the console.
 *
 * Each discovery track's novelty rate per round, against the saturation
 * threshold. Saturation (not budget, not elapsed time) is the harness's real
 * stop condition, so watching both traces fall through the threshold line IS
 * watching the run decide it is finished.
 *
 * Two series → legend always present, plus direct end-labels. One shared
 * y-axis; the two series are the same measure, so a second scale would be a
 * lie about their comparability.
 * ----------------------------------------------------------------------- */

export interface NoveltySeries {
  key: string;
  label: string;
  color: string;
  values: number[];
}

export function NoveltyDecay({
  series,
  threshold,
  height = 190,
}: {
  series: NoveltySeries[];
  threshold: number;
  height?: number;
}) {
  const clipId = useId();
  const [hoverRound, setHoverRound] = useState<number | null>(null);

  const rounds = Math.max(...series.map((s) => s.values.length), 0);
  const present = series.filter((s) => s.values.length > 0);

  const maxValue = useMemo(() => {
    const dataMax = Math.max(...present.flatMap((s) => s.values), threshold);
    // Headroom so the top trace never rides the frame.
    return dataMax > 0 ? dataMax * 1.15 : 1;
  }, [present, threshold]);

  if (rounds === 0 || present.length === 0) {
    return (
      <div className="flex items-center justify-center px-4 text-xs text-ink-3" style={{ height }}>
        No rounds recorded yet — novelty appears once a node has run both tracks.
      </div>
    );
  }

  const pad = { top: 10, right: 52, bottom: 22, left: 34 };
  const width = 620;
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const x = (i: number) => pad.left + (rounds === 1 ? plotW / 2 : (i / (rounds - 1)) * plotW);
  const y = (v: number) => pad.top + plotH - (v / maxValue) * plotH;

  const ticks = [0, maxValue / 2, maxValue];

  return (
    <figure className="m-0">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full"
        style={{ height }}
        role="img"
        aria-label={`Novelty rate per round for ${present.map((s) => s.label).join(' and ')}, against a saturation threshold of ${threshold}`}
        onMouseLeave={() => setHoverRound(null)}
      >
        <defs>
          <clipPath id={clipId}>
            <rect x={pad.left} y={pad.top} width={plotW} height={plotH} />
          </clipPath>
        </defs>

        {/* Recessive grid + axis labels */}
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={pad.left}
              x2={pad.left + plotW}
              y1={y(t)}
              y2={y(t)}
              stroke="var(--grid)"
              strokeWidth="1"
            />
            <text
              x={pad.left - 7}
              y={y(t) + 3}
              textAnchor="end"
              className="tnum fill-ink-3 font-mono"
              style={{ fontSize: 9 }}
            >
              {decimal(t, 2)}
            </text>
          </g>
        ))}

        {/* Saturation threshold — a reference rule, not a series. Dashed so it
            never reads as data. */}
        <line
          x1={pad.left}
          x2={pad.left + plotW}
          y1={y(threshold)}
          y2={y(threshold)}
          stroke="var(--ink-3)"
          strokeWidth="1.5"
          strokeDasharray="4 3"
        />
        <text
          x={pad.left + plotW + 6}
          y={y(threshold) + 3}
          className="fill-ink-3 font-mono"
          style={{ fontSize: 9 }}
        >
          saturate
        </text>

        {present.map((s) => {
          const pts = s.values.map((v, i) => `${x(i)},${y(v)}`).join(' ');
          const lastIdx = s.values.length - 1;
          return (
            <g key={s.key} clipPath={`url(#${clipId})`}>
              <polyline
                points={pts}
                fill="none"
                stroke={s.color}
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              {s.values.map((v, i) => (
                <circle
                  key={i}
                  cx={x(i)}
                  cy={y(v)}
                  r={hoverRound === i ? 4.5 : 3}
                  fill={s.color}
                  stroke="var(--surface)"
                  strokeWidth="2"
                />
              ))}
              {lastIdx >= 0 ? (
                <text
                  x={Math.min(x(lastIdx) + 8, width - 4)}
                  y={y(s.values[lastIdx]) + 3}
                  className="tnum fill-ink-2 font-mono"
                  style={{ fontSize: 9 }}
                >
                  {decimal(s.values[lastIdx], 2)}
                </text>
              ) : null}
            </g>
          );
        })}

        {/* Crosshair + oversized hit targets */}
        {Array.from({ length: rounds }).map((_, i) => (
          <rect
            key={i}
            x={x(i) - plotW / Math.max(rounds, 1) / 2}
            y={pad.top}
            width={plotW / Math.max(rounds, 1) || 12}
            height={plotH}
            fill="transparent"
            onMouseEnter={() => setHoverRound(i)}
          />
        ))}
        {hoverRound !== null ? (
          <line
            x1={x(hoverRound)}
            x2={x(hoverRound)}
            y1={pad.top}
            y2={pad.top + plotH}
            stroke="var(--ink-3)"
            strokeWidth="1"
          />
        ) : null}

        <text
          x={pad.left}
          y={height - 5}
          className="fill-ink-3 font-mono"
          style={{ fontSize: 9 }}
        >
          round 1
        </text>
        <text
          x={pad.left + plotW}
          y={height - 5}
          textAnchor="end"
          className="fill-ink-3 font-mono"
          style={{ fontSize: 9 }}
        >
          round {rounds}
        </text>
      </svg>

      <figcaption className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 px-1">
        {present.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1.5 text-xs text-ink-2">
            <span
              aria-hidden
              className="block h-[3px] w-3.5 rounded-full"
              style={{ background: s.color }}
            />
            {s.label}
            {hoverRound !== null && s.values[hoverRound] !== undefined ? (
              <span className="tnum font-mono text-ink">{decimal(s.values[hoverRound], 3)}</span>
            ) : null}
          </span>
        ))}
        <span className="inline-flex items-center gap-1.5 text-xs text-ink-3">
          <span
            aria-hidden
            className="block h-0 w-3.5 border-t-[1.5px] border-dashed"
            style={{ borderColor: 'var(--ink-3)' }}
          />
          threshold {decimal(threshold, 2)}
        </span>
      </figcaption>
    </figure>
  );
}

/* --------------------------------------------------------------------------
 * BarList — magnitude across a handful of named rows.
 *
 * Bars are anchored to a common baseline with rounded data-ends and a 2px
 * surface gap; values are direct-labelled, so no axis is needed.
 * ----------------------------------------------------------------------- */

export interface BarRow {
  key: string;
  label: string;
  value: number;
  display: string;
  color?: string;
  note?: string;
}

export function BarList({ rows, emptyLabel = 'No data yet.' }: { rows: BarRow[]; emptyLabel?: string }) {
  const max = Math.max(...rows.map((r) => r.value), 0);

  if (rows.length === 0) {
    return <p className="px-4 py-8 text-center text-xs text-ink-3">{emptyLabel}</p>;
  }

  return (
    <ul className="divide-y divide-line">
      {rows.map((r) => {
        const pct = max > 0 ? (r.value / max) * 100 : 0;
        return (
          <li key={r.key} className="px-4 py-2.5">
            <div className="flex items-baseline justify-between gap-3">
              <span className="truncate font-mono text-xs text-ink">{r.label}</span>
              <span className="tnum shrink-0 font-mono text-xs text-ink-2">{r.display}</span>
            </div>
            <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-sunken">
              <div
                className="h-full rounded-full"
                style={{
                  width: `${Math.max(pct, r.value > 0 ? 2 : 0)}%`,
                  background: r.color ?? 'var(--track-graph)',
                }}
              />
            </div>
            {r.note ? <p className="mt-1 text-[11px] text-ink-3">{r.note}</p> : null}
          </li>
        );
      })}
    </ul>
  );
}

/* --------------------------------------------------------------------------
 * Sparkline — inline trend, no axes, no hover. Deliberately bare: it is a
 * glance cue inside a row, and the full chart is one click away.
 * ----------------------------------------------------------------------- */

export function Sparkline({
  values,
  color = 'var(--track-graph)',
  width = 68,
  height = 18,
}: {
  values: number[];
  color?: string;
  width?: number;
  height?: number;
}) {
  if (values.length < 2) return <span className="text-xs text-ink-3">—</span>;

  const max = Math.max(...values, 0.0001);
  const pts = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * (width - 2) + 1;
      const y = height - 2 - (v / max) * (height - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');

  return (
    <svg width={width} height={height} aria-hidden className="overflow-visible">
      <polyline
        points={pts}
        fill="none"
        stroke={color}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
