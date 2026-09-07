import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import type { DiscoveryMethod, Grade, RunStatus } from '../lib/api';

/* --------------------------------------------------------------------------
 * Surfaces
 * ----------------------------------------------------------------------- */

export function Panel({
  children,
  className = '',
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-lg border border-line bg-surface ${className}`}
      style={{ boxShadow: 'var(--shadow)' }}
    >
      {children}
    </section>
  );
}

export function PanelHeader({
  title,
  hint,
  right,
}: {
  title: string;
  hint?: string;
  right?: ReactNode;
}) {
  return (
    <header className="flex items-baseline justify-between gap-4 border-b border-line px-4 py-3">
      <div className="min-w-0">
        <h2 className="font-display text-sm font-semibold tracking-tight text-ink">{title}</h2>
        {hint ? <p className="mt-0.5 text-xs text-ink-3">{hint}</p> : null}
      </div>
      {right ? <div className="shrink-0">{right}</div> : null}
    </header>
  );
}

/* --------------------------------------------------------------------------
 * Eyebrow — a small uppercase label. Used only where it names a real
 * category, never as decoration.
 * ----------------------------------------------------------------------- */

/* --------------------------------------------------------------------------
 * Tooltip
 *
 * The `title` attribute draws the browser's own tooltip: an OS-styled yellow
 * box in a system font, on the browser's delay, ignoring the theme entirely
 * and unreadable against the dark palette. This is the same information in
 * the platform's own surface, type and elevation.
 *
 * CSS-only, on hover and on keyboard focus. The trigger takes a tabIndex so
 * the text is reachable without a pointer, and `aria-describedby` ties the
 * two together for screen readers, which is what `title` was quietly doing.
 * ----------------------------------------------------------------------- */

let tooltipSeq = 0;

type TooltipSide = 'top' | 'bottom' | 'right' | 'left';

// Placement of the bubble and its arrow, per side. The arrow is a rotated
// square clipped by two borders, so which two borders show is what makes it
// point the right way from each side.
const TOOLTIP_PLACEMENT: Record<
  TooltipSide,
  { bubble: string; arrow: string }
> = {
  top: {
    bubble: 'bottom-full left-1/2 mb-2 -translate-x-1/2',
    arrow: 'left-1/2 top-full -mt-1 -translate-x-1/2 border-r border-b',
  },
  bottom: {
    bubble: 'top-full left-1/2 mt-2 -translate-x-1/2',
    arrow: 'left-1/2 bottom-full -mb-1 -translate-x-1/2 border-t border-l',
  },
  right: {
    bubble: 'left-full top-1/2 ml-2 -translate-y-1/2',
    arrow: 'right-full top-1/2 -mr-1 -translate-y-1/2 border-b border-l',
  },
  left: {
    bubble: 'right-full top-1/2 mr-2 -translate-y-1/2',
    arrow: 'left-full top-1/2 -ml-1 -translate-y-1/2 border-t border-r',
  },
};

export function Tooltip({
  label,
  children,
  className = '',
  side = 'top',
}: {
  label?: string;
  children: ReactNode;
  className?: string;
  side?: TooltipSide;
}) {
  // Stable for the life of the component; only ever an id, never rendered.
  const [id] = useState(() => `tt-${++tooltipSeq}`);

  if (!label) return <>{children}</>;

  const placement = TOOLTIP_PLACEMENT[side];
  return (
    <span
      className={`group/tt relative inline-flex ${className}`}
      tabIndex={0}
      aria-describedby={id}
    >
      {children}
      <span
        role="tooltip"
        id={id}
        className={`pointer-events-none absolute z-30 w-max max-w-64 scale-95
          rounded-md border border-line bg-raised px-2.5 py-1.5 text-left text-xs leading-snug
          text-ink-2 opacity-0 transition-[opacity,transform] duration-100
          group-hover/tt:scale-100 group-hover/tt:opacity-100
          group-focus-visible/tt:scale-100 group-focus-visible/tt:opacity-100
          ${placement.bubble}`}
        style={{ boxShadow: 'var(--shadow-pop)' }}
      >
        {label}
        <span
          aria-hidden
          className={`absolute size-2 rotate-45 border-line bg-raised ${placement.arrow}`}
        />
      </span>
    </span>
  );
}

/* --------------------------------------------------------------------------
 * Destructive action
 *
 * A real dialog, not a two-click button. An armed button says "Really?" in
 * the same place the reader just clicked and disappears on a timer -- it
 * asks the question quietly, next to a dozen other controls, and a reader
 * skimming can commit to a delete without ever registering that they were
 * asked. A dialog takes the centre of the screen, names the specific thing
 * being deleted, and cannot be dismissed by accident.
 *
 * Rendered inline rather than through a portal: the app has no stacking
 * context deeper than the shell, and a fixed overlay at z-50 clears
 * everything on these pages.
 * ----------------------------------------------------------------------- */

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = 'Delete',
  pending = false,
  pendingLabel = 'Deleting…',
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  pending?: boolean;
  pendingLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  // Escape closes it. A modal that traps a reader who changed their mind is
  // worse than the accidental click it was meant to prevent.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !pending) onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, pending, onCancel]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <button
        type="button"
        aria-label="Cancel"
        onClick={() => !pending && onCancel()}
        className="absolute inset-0 cursor-default bg-black/50"
      />
      <div
        className="relative w-full max-w-md rounded-lg border border-line bg-surface p-5"
        style={{ boxShadow: 'var(--shadow-pop)' }}
      >
        <h2 className="font-display text-base font-semibold tracking-tight text-ink">
          {title}
        </h2>
        <div className="mt-2 text-sm leading-snug text-ink-2">{body}</div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={pending}
            className="rounded-md border border-line px-3 py-1.5 text-sm text-ink-2 transition-colors hover:bg-sunken hover:text-ink disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={pending}
            autoFocus
            className="rounded-md px-3 py-1.5 text-sm font-medium text-white transition-opacity disabled:opacity-50"
            style={{ background: 'var(--status-critical)' }}
          >
            {pending ? pendingLabel : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

/** The button that opens the dialog. Styled as destructive but harmless. */
export function DangerButton({
  onClick,
  children,
  title,
  className = '',
}: {
  onClick: () => void;
  children: ReactNode;
  title?: string;
  className?: string;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className={`rounded-md border border-line px-2 py-1 text-xs text-ink-3 transition-colors hover:border-[var(--status-critical)] hover:text-[var(--status-critical)] ${className}`}
    >
      {children}
    </button>
  );
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-3">{children}</span>
  );
}

/* --------------------------------------------------------------------------
 * Stat tile — a figure with its label. No plot, so no hover layer.
 * ----------------------------------------------------------------------- */

export function StatTile({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: ReactNode;
  sub?: string;
  accent?: string;
}) {
  return (
    <div className="rounded-lg border border-line bg-surface px-4 py-3">
      <Eyebrow>{label}</Eyebrow>
      <div
        className="mt-1.5 font-display text-2xl leading-none font-semibold"
        style={accent ? { color: accent } : undefined}
      >
        {value}
      </div>
      {sub ? <p className="mt-1.5 text-xs text-ink-3">{sub}</p> : null}
    </div>
  );
}

/* --------------------------------------------------------------------------
 * Evidence grade.
 *
 * Grade is ordinal, so it takes a single-hue ramp (strong = full signal,
 * weak = receded) rather than a traffic light. Colour never carries it alone:
 * every badge ships the word and a filled-pip count, which is what survives
 * greyscale, CVD, and forced-colors.
 * ----------------------------------------------------------------------- */

export const GRADE_COLOR: Record<Grade, string> = {
  strong: 'var(--grade-strong)',
  moderate: 'var(--grade-moderate)',
  weak: 'var(--grade-weak)',
};

const GRADE_PIPS: Record<Grade, number> = { strong: 3, moderate: 2, weak: 1 };

const GRADE_ORDER: Grade[] = ['strong', 'moderate', 'weak'];

/** Compact "N strong · N moderate · N weak" summary for a report card —
 * zero-count grades are omitted rather than cluttering the row with "0". */
export function GradeTally({ counts }: { counts: Record<Grade, number> }) {
  const present = GRADE_ORDER.filter((g) => counts[g] > 0);
  if (present.length === 0) {
    return <span className="text-xs text-ink-3">No findings</span>;
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-x-3 gap-y-1">
      {present.map((grade) => (
        <span
          key={grade}
          className="inline-flex items-center gap-1.5 font-mono text-xs"
          style={{ color: GRADE_COLOR[grade] }}
        >
          <span aria-hidden className="h-1.5 w-1.5 rounded-full" style={{ background: GRADE_COLOR[grade] }} />
          {counts[grade]} {grade}
        </span>
      ))}
    </span>
  );
}

export function GradeBadge({ grade, size = 'md' }: { grade: Grade; size?: 'sm' | 'md' }) {
  const color = GRADE_COLOR[grade];
  const pips = GRADE_PIPS[grade];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 font-mono uppercase tracking-wide ${
        size === 'sm' ? 'text-[10px]' : 'text-[11px]'
      }`}
      style={{
        color,
        borderColor: `color-mix(in oklab, ${color} 45%, transparent)`,
        background: `color-mix(in oklab, ${color} 10%, transparent)`,
      }}
    >
      <span aria-hidden className="flex gap-[2px]">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="block h-[7px] w-[3px] rounded-[1px]"
            style={{ background: i < pips ? color : 'currentColor', opacity: i < pips ? 1 : 0.22 }}
          />
        ))}
      </span>
      {grade}
    </span>
  );
}

/* --------------------------------------------------------------------------
 * Discovery track.
 *
 * The one encoding the product is actually about: "graph_walk" means the
 * keyword track never returned this channel. Shape backs up colour — filled
 * for graph-walk-only, hollow for keyword-findable — so the distinction holds
 * without colour.
 * ----------------------------------------------------------------------- */

export const TRACK_META: Record<
  DiscoveryMethod,
  { label: string; color: string; filled: boolean; note: string }
> = {
  graph_walk: {
    label: 'Graph walk only',
    color: 'var(--track-graph)',
    filled: true,
    note: 'Keyword search never returned this channel.',
  },
  both: {
    label: 'Both tracks',
    color: 'var(--track-seed)',
    filled: true,
    note: 'Found by keyword search and by the graph walk.',
  },
  keyword: {
    label: 'Keyword',
    color: 'var(--track-keyword)',
    filled: false,
    note: 'Reachable by plain keyword search.',
  },
  unattributed: {
    label: 'Unattributed',
    color: 'var(--ink-3)',
    filled: false,
    note: 'Discovered before per-track attribution existed (checkpoint schema < 4).',
  },
  unhydrated: {
    label: 'Not hydrated',
    color: 'var(--ink-3)',
    filled: false,
    note: 'Seen as a graph edge but never fetched from the YouTube API.',
  },
};

export function TrackMark({ method, size = 9 }: { method: DiscoveryMethod; size?: number }) {
  const meta = TRACK_META[method] ?? TRACK_META.unattributed;
  return (
    <span
      aria-hidden
      className="inline-block shrink-0 rounded-full"
      style={{
        width: size,
        height: size,
        background: meta.filled ? meta.color : 'transparent',
        border: `1.5px solid ${meta.color}`,
      }}
    />
  );
}

export function TrackBadge({ method }: { method: DiscoveryMethod }) {
  const meta = TRACK_META[method] ?? TRACK_META.unattributed;
  return (
    <span className="inline-flex items-center gap-1.5 text-xs whitespace-nowrap text-ink-2">
      <TrackMark method={method} />
      {meta.label}
    </span>
  );
}

/* --------------------------------------------------------------------------
 * Run status
 * ----------------------------------------------------------------------- */

const STATUS_META: Record<RunStatus, { label: string; color: string; pulse: boolean }> = {
  running: { label: 'Running', color: 'var(--track-graph)', pulse: true },
  complete: { label: 'Finished', color: 'var(--status-good)', pulse: false },
  stopped: { label: 'Stopped', color: 'var(--status-critical)', pulse: false },
  pending: { label: 'Pending', color: 'var(--ink-3)', pulse: false },
};

export function StatusPill({ status }: { status: RunStatus }) {
  const meta = STATUS_META[status] ?? STATUS_META.pending;
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-[11px] whitespace-nowrap text-ink-2">
      <span aria-hidden className="relative flex h-2 w-2">
        {meta.pulse ? (
          <span
            className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60 motion-reduce:animate-none"
            style={{ background: meta.color }}
          />
        ) : null}
        <span
          className="relative inline-flex h-2 w-2 rounded-full"
          style={{ background: meta.color }}
        />
      </span>
      {meta.label}
    </span>
  );
}

/* --------------------------------------------------------------------------
 * States
 * ----------------------------------------------------------------------- */

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="px-4 py-12 text-center">
      <p className="font-display text-sm font-medium text-ink-2">{title}</p>
      {children ? <div className="mt-1.5 text-xs text-ink-3">{children}</div> : null}
    </div>
  );
}

export function ErrorState({ title, detail }: { title: string; detail?: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border px-4 py-3"
      style={{
        borderColor: 'color-mix(in oklab, var(--status-critical) 40%, transparent)',
        background: 'color-mix(in oklab, var(--status-critical) 8%, transparent)',
      }}
    >
      <p className="flex items-center gap-2 text-sm font-medium text-ink">
        <svg aria-hidden width="14" height="14" viewBox="0 0 16 16" fill="none">
          <path
            d="M8 1.5 15 14H1L8 1.5Z"
            stroke="var(--status-critical)"
            strokeWidth="1.5"
            strokeLinejoin="round"
          />
          <path d="M8 6.5v3.2" stroke="var(--status-critical)" strokeWidth="1.5" />
          <circle cx="8" cy="11.7" r="0.9" fill="var(--status-critical)" />
        </svg>
        {title}
      </p>
      {detail ? <p className="mt-1 font-mono text-xs break-words text-ink-3">{detail}</p> : null}
    </div>
  );
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2 px-4 py-4" aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-8 overflow-hidden rounded bg-sunken">
          <div
            className="h-full w-full opacity-40"
            style={{
              background:
                'linear-gradient(180deg, transparent, color-mix(in oklab, var(--track-graph) 22%, transparent), transparent)',
              animation: 'sweep 1.4s ease-in-out infinite',
            }}
          />
        </div>
      ))}
    </div>
  );
}
