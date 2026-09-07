import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import { workbookStamp } from '../lib/format';

/**
 * Which finished deliverable a page is about.
 *
 * The console is organised around workbooks rather than runs: a workbook is
 * built from many run_ids unioned together, so "which run produced
 * finance.xlsx" has no single answer. Every page that reports on finished
 * work asks this question the same way.
 */
export function WorkbookPicker({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (id: string) => void;
}) {
  const workbooks = useQuery({ queryKey: ['workbooks'], queryFn: api.workbooks });
  const available = (workbooks.data ?? []).filter((w) => w.available);

  // A topic run twice produces two deliverables with the same name, and the
  // console listed three "Crime" tabs with nothing to tell them apart. The
  // stamp is shown only where it is needed to choose, so a workbook with a
  // unique name is not cluttered by a date nobody has to read.
  const seen = new Map<string, number>();
  for (const w of available) seen.set(w.title, (seen.get(w.title) ?? 0) + 1);
  const ambiguous = (title: string) => (seen.get(title) ?? 0) > 1;

  if (workbooks.isLoading) {
    return <div className="h-9 w-56 animate-pulse rounded-md bg-sunken" />;
  }
  if (available.length === 0) {
    return (
      <p className="text-sm text-ink-3">
        Nothing to show yet — this fills in once a run finishes.
      </p>
    );
  }

  return (
    <div role="tablist" aria-label="Workbook" className="flex gap-1 rounded-lg bg-sunken p-1">
      {available.map((w) => {
        const active = value === w.id;
        const stamp = ambiguous(w.title) ? workbookStamp(w.modified_at) : '';
        return (
          <button
            key={w.id}
            role="tab"
            aria-selected={active}
            // Spelled out for a screen reader, which cannot see that the
            // small line below the title is what distinguishes two tabs.
            aria-label={
              stamp
                ? `${w.title}, exported ${stamp}, ${w.channel_count ?? 0} channels`
                : `${w.title}, ${w.channel_count ?? 0} channels`
            }
            onClick={() => onChange(w.id)}
            className={`rounded-md px-3 py-1.5 text-left text-sm transition-colors ${
              active
                ? 'bg-raised font-medium text-ink shadow-[var(--shadow)]'
                : 'text-ink-2 hover:text-ink'
            }`}
          >
            <span className="flex items-baseline gap-2">
              <span>{w.title}</span>
              <span className="tabular-nums text-xs text-ink-3">
                {w.channel_count?.toLocaleString() ?? '—'}
              </span>
            </span>
            {stamp && (
              <span className="block text-[11px] leading-tight tabular-nums text-ink-3">
                {stamp}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
