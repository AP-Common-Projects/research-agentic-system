import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError, type Run } from '../lib/api';
import {
  EmptyState,
  ErrorState,
  Eyebrow,
  Panel,
  PanelHeader,
  Skeleton,
  StatusPill,
} from '../components/primitives';
import { relativeTime, usd } from '../lib/format';

const CATEGORIES = ['Finance', 'Legal'] as const;
type Category = (typeof CATEGORIES)[number];

function LaunchForm() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Category | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const launch = useMutation({
    mutationFn: (niches: string[]) => api.launchRun(niches),
    onSuccess: () => {
      setSelected(null);
      setFormError(null);
      queryClient.invalidateQueries({ queryKey: ['runs'] });
    },
    onError: (err) =>
      setFormError(err instanceof ApiError ? err.message : 'Could not start the run.'),
  });

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!selected) {
      setFormError('Pick a category to research.');
      return;
    }
    setFormError(null);
    launch.mutate([selected]);
  }

  return (
    <Panel>
      <PanelHeader title="Start a run" hint="Each run researches one category." />
      <form onSubmit={onSubmit} className="space-y-3 px-4 py-4">
        <div>
          <span className="mb-1.5 block text-xs font-medium text-ink-2">Category</span>
          <div className="grid grid-cols-2 gap-2">
            {CATEGORIES.map((category) => {
              const isSelected = selected === category;
              return (
                <label
                  key={category}
                  className="flex cursor-pointer items-center gap-2 rounded-md border px-3 py-3 transition-colors has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-[var(--focus)]"
                  style={{
                    borderColor: isSelected ? 'var(--track-graph)' : 'var(--line)',
                    background: isSelected
                      ? 'color-mix(in oklab, var(--track-graph) 10%, transparent)'
                      : 'var(--page)',
                  }}
                >
                  <input
                    type="radio"
                    name="category"
                    value={category}
                    checked={isSelected}
                    onChange={() => setSelected(category)}
                    className="sr-only"
                  />
                  <span
                    aria-hidden
                    className="h-3 w-3 shrink-0 rounded-full border-[1.5px]"
                    style={{
                      borderColor: isSelected ? 'var(--track-graph)' : 'var(--ink-3)',
                      background: isSelected ? 'var(--track-graph)' : 'transparent',
                    }}
                  />
                  <span className="font-display text-sm font-medium text-ink">{category}</span>
                </label>
              );
            })}
          </div>
        </div>

        <button
          type="submit"
          disabled={launch.isPending}
          className="rounded-md px-4 py-2 text-sm font-medium text-page transition-opacity hover:opacity-90 disabled:opacity-50"
          style={{ background: 'var(--track-graph)', touchAction: 'manipulation' }}
        >
          {launch.isPending ? 'Starting…' : 'Start run'}
        </button>

        <p className="text-[11px] text-ink-3">
          Saturation, not time or budget, is the stop condition.
        </p>

        {formError ? <ErrorState title="Run not started" detail={formError} /> : null}
      </form>
    </Panel>
  );
}

function RunRow({ run }: { run: Run }) {
  return (
    <li>
      <Link
        to={`/runs/${run.run_id}`}
        className="grid grid-cols-[1fr_auto] items-center gap-4 px-4 py-3 transition-colors hover:bg-sunken"
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <StatusPill status={run.status} />
            <span className="truncate font-display text-sm font-medium text-ink">
              {run.niches.length > 0 ? run.niches.join(', ') : run.run_id}
            </span>
          </div>
          <p className="mt-1 font-mono text-[11px] text-ink-3">
            {run.run_id}
            {run.last_node ? ` · ${run.last_node}` : ''}
            {run.last_activity_at ? ` · ${relativeTime(run.last_activity_at)}` : ''}
          </p>
        </div>
        <div className="text-right">
          <div className="tnum font-mono text-sm text-ink">{usd(run.cost_usd)}</div>
          <div className="tnum font-mono text-[11px] text-ink-3">{run.log_lines} steps</div>
        </div>
      </Link>
    </li>
  );
}

export function RunsPage() {
  const runs = useQuery({
    queryKey: ['runs'],
    queryFn: api.runs,
    refetchInterval: 5_000,
  });

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <header className="mb-6">
        <Eyebrow>Console</Eyebrow>
        <h1 className="mt-1 font-display text-2xl font-semibold tracking-tight text-balance text-ink">Runs</h1>
      </header>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
        <Panel>
          <PanelHeader
            title="History"
            hint="Runs started here and from the command line."
            right={
              runs.isFetching ? <span className="font-mono text-[10px] text-ink-3">syncing</span> : null
            }
          />
          {runs.isLoading ? (
            <Skeleton rows={4} />
          ) : runs.isError ? (
            <div className="p-4">
              <ErrorState
                title="Could not load runs"
                detail={runs.error instanceof Error ? runs.error.message : undefined}
              />
            </div>
          ) : runs.data && runs.data.length > 0 ? (
            <ul className="divide-y divide-line">
              {runs.data.map((run) => (
                <RunRow key={run.run_id} run={run} />
              ))}
            </ul>
          ) : (
            <EmptyState title="No runs yet">
              Start one with the form, or run{' '}
              <code className="font-mono">python -m src.cli "Finance"</code> — both land here.
            </EmptyState>
          )}
        </Panel>

        <LaunchForm />
      </div>
    </div>
  );
}
