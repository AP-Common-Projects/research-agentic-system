import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';

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
        return (
          <button
            key={w.id}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(w.id)}
            className={`rounded-md px-3 py-1.5 text-sm transition-colors ${
              active
                ? 'bg-raised font-medium text-ink shadow-[var(--shadow)]'
                : 'text-ink-2 hover:text-ink'
            }`}
          >
            {w.title}
            <span className="ml-2 tabular-nums text-xs text-ink-3">
              {w.channel_count?.toLocaleString() ?? '—'}
            </span>
          </button>
        );
      })}
    </div>
  );
}
