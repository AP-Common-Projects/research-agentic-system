import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type Run } from '../lib/api';
import { useRunEvents } from '../hooks/useRunEvents';
import {
  Panel,
  PanelHeader,
  Eyebrow,
  StatTile,
  StatusPill,
  EmptyState,
  ErrorState,
  Skeleton,
  Tooltip,
} from '../components/primitives';

/* --------------------------------------------------------------------------
 * A run is a chain of graph nodes, each logged as it completes. The stream is
 * the monitor: nothing else says what the harness is doing *right now*.
 * ----------------------------------------------------------------------- */

/** What each node is, in the client's terms rather than the graph's. */
const NODE_META: Record<string, { label: string; phase: Phase }> = {
  scan_niches: { label: 'Reading the topic', phase: 'plan' },
  expand_niche_adjacency: { label: 'Looking for adjacent areas', phase: 'plan' },
  build_taxonomy: { label: 'Mapping sub-niches', phase: 'plan' },
  select_next_node: { label: 'Choosing the next branch', phase: 'plan' },
  underperformer_discovery: { label: 'Sweeping smaller channels', phase: 'discover' },
  graph_walk: { label: 'Following channel references', phase: 'discover' },
  keyword_search: { label: 'Searching keywords', phase: 'discover' },
  breakout_scanner: { label: 'Scanning for breakouts', phase: 'discover' },
  new_channel_discovery: { label: 'Finding new channels', phase: 'discover' },
  hydrate_metadata: { label: 'Pulling channel details', phase: 'enrich' },
  resolve_geo_language: { label: 'Resolving region and language', phase: 'enrich' },
  extract_metadata_signals: { label: 'Reading metadata signals', phase: 'enrich' },
  check_saturation: { label: 'Checking for saturation', phase: 'assess' },
  cluster_branch: { label: 'Clustering the branch', phase: 'assess' },
  compact_branch: { label: 'Summarising the branch', phase: 'assess' },
  finalize_dataset: { label: 'Finalising the dataset', phase: 'finish' },
  synthesize: { label: 'Writing the report', phase: 'finish' },
};

type Phase = 'plan' | 'discover' | 'enrich' | 'assess' | 'finish';

const PHASE_COLOR: Record<Phase, string> = {
  plan: 'var(--track-seed)',
  discover: 'var(--track-keyword)',
  enrich: 'var(--track-graph)',
  assess: 'var(--grade-moderate)',
  finish: 'var(--status-good)',
};

function nodeMeta(name: string) {
  return NODE_META[name] ?? { label: name.replace(/_/g, ' '), phase: 'plan' as Phase };
}

function usd(n: number | null | undefined): string {
  if (n == null) return '—';
  if (n === 0) return '$0';
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

/** Wall-clock since a timestamp, in the largest unit that stays readable. */
function elapsed(fromIso: string, toIso?: string | null): string {
  const start = new Date(fromIso).getTime();
  const end = toIso ? new Date(toIso).getTime() : Date.now();
  const s = Math.max(0, Math.round((end - start) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** The numbers a node reports are its own shape; show the ones that mean
 *  something to a reader and skip the bookkeeping. */
const INTERESTING_KEYS = new Set([
  'channels_found', 'new', 'found', 'records_consumed', 'records', 'queries_run',
  'queries', 'edges_found', 'edges_written', 'novelty', 'channel_count',
  'video_count', 'resolved', 'total', 'processed', 'decision', 'reason',
  'breakout_channels_found', 'new_discoveries', 'results', 'discovered',
  'selected_niche', 'niche', 'node_id', 'rounds', 'spent_usd',
]);

function summarise(input: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(input ?? {})) {
    if (!INTERESTING_KEYS.has(k)) continue;
    if (v === null || v === undefined || v === '') continue;
    if (Array.isArray(v)) continue;
    if (typeof v === 'object') continue;
    parts.push(`${k.replace(/_/g, ' ')}: ${v}`);
  }
  return parts.join(' · ');
}

/* ---- the stream ------------------------------------------------------- */

function ActivityStream({ run }: { run: Run }) {
  const isLive = run.status === 'running';
  const { entries, connection } = useRunEvents(run.run_id, isLive);

  // Newest first: on a run that has been going for an hour, what just
  // happened is the thing being monitored, and it should not require a
  // scroll to the bottom of two hundred rows to see it.
  const ordered = useMemo(() => [...entries].reverse(), [entries]);

  if (connection === 'connecting' && entries.length === 0) {
    return <div className="p-4"><Skeleton rows={4} /></div>;
  }

  if (entries.length === 0) {
    return (
      <EmptyState title="Nothing logged yet">
        The first step takes a moment to report. This fills in as the run
        works through the topic.
      </EmptyState>
    );
  }

  return (
    <ol className="divide-y divide-line">
      {ordered.map((entry, i) => {
        const meta = nodeMeta(entry.node_name);
        const detail = summarise(entry.input_summary);
        // Only the newest row can be in progress, and only while live.
        const isCurrent = i === 0 && isLive;
        return (
          <li
            key={`${entry.timestamp}-${entry.node_name}-${i}`}
            className={`flex items-start gap-3 px-4 py-2.5 ${isCurrent ? 'bg-sunken' : ''}`}
          >
            <Tooltip label={`${entry.node_name} · ${meta.phase}`}>
              <span
                aria-hidden
                className="mt-1.5 size-2 shrink-0 rounded-full"
                style={{ background: PHASE_COLOR[meta.phase] }}
              />
            </Tooltip>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className={`text-sm ${isCurrent ? 'font-medium text-ink' : 'text-ink-2'}`}>
                  {meta.label}
                </span>
                <span className="font-mono text-[10px] text-ink-3">
                  {new Date(entry.timestamp).toLocaleTimeString()}
                </span>
              </div>
              {detail && (
                <p className="mt-0.5 font-mono text-[11px] leading-snug break-words text-ink-3">
                  {detail}
                </p>
              )}
            </div>
            <div className="shrink-0 text-right">
              {entry.cost_usd ? (
                <span className="font-mono text-[11px] tabular-nums text-ink-3">
                  {usd(entry.cost_usd)}
                </span>
              ) : null}
              {entry.latency_ms != null && entry.latency_ms > 1000 ? (
                <p className="font-mono text-[10px] tabular-nums text-ink-3">
                  {(entry.latency_ms / 1000).toFixed(1)}s
                </p>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/* ---- the selected run ------------------------------------------------- */

function RunDetail({ run }: { run: Run }) {
  const isLive = run.status === 'running';

  // The checkpoint carries the counts the log lines do not. Polled rather
  // than streamed: it is a Postgres read per call, and these numbers move on
  // the scale of minutes.
  const detail = useQuery({
    queryKey: ['run-detail', run.run_id],
    queryFn: () => api.run(run.run_id),
    refetchInterval: isLive ? 15_000 : false,
  });

  const state = detail.data?.state ?? null;
  const errors = state?.errors ?? [];

  // Re-render on a timer so "running for 4m 12s" stays true without a
  // request. Only while live -- a finished run's elapsed time is fixed.
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!isLive) return;
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, [isLive]);

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          label="Elapsed"
          value={elapsed(run.started_at, isLive ? null : run.last_activity_at)}
          sub={isLive ? 'still running' : 'final'}
        />
        <StatTile
          label="Channels"
          value={state?.discovered_channel_count ?? '—'}
          sub={state ? `${state.discovered_video_count.toLocaleString()} videos` : 'awaiting first checkpoint'}
        />
        <StatTile
          label="Spent"
          value={usd(state?.budget_spent_usd ?? run.cost_usd)}
          sub="model + discovery"
        />
        <StatTile
          label="Steps"
          value={run.log_lines}
          sub={run.last_node ? nodeMeta(run.last_node).label : 'starting'}
        />
      </div>

      {errors.length > 0 && (
        <Panel>
          <PanelHeader title={`Errors (${errors.length})`} hint="Reported by the run itself" />
          <ul className="divide-y divide-line">
            {errors.slice(-5).map((e, i) => (
              <li key={i} className="px-4 py-2.5">
                <p className="text-sm text-ink">
                  {nodeMeta(e.node_name).label}
                  <span className="ml-2 font-mono text-[11px] text-[var(--status-critical)]">
                    {e.error_type}
                  </span>
                </p>
                <p className="mt-0.5 font-mono text-[11px] leading-snug break-words text-ink-3">
                  {e.message}
                </p>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <Panel>
        <PanelHeader
          title="Activity"
          hint={isLive ? 'Streaming live, newest first' : 'Newest first'}
        />
        <ActivityStream run={run} />
      </Panel>
    </div>
  );
}

/* ---- the page --------------------------------------------------------- */

export function LiveRunsPage() {
  const runs = useQuery({
    queryKey: ['runs'],
    queryFn: api.runs,
    // Status changes (running -> complete) arrive here, not on the stream.
    refetchInterval: 5_000,
  });

  const [selected, setSelected] = useState<string | null>(null);

  const ordered = useMemo(() => {
    const all = runs.data ?? [];
    // Live runs first -- this page exists for those -- then most recent.
    return [...all].sort((a, b) => {
      const liveA = a.status === 'running' ? 0 : 1;
      const liveB = b.status === 'running' ? 0 : 1;
      if (liveA !== liveB) return liveA - liveB;
      return (b.started_at || '').localeCompare(a.started_at || '');
    });
  }, [runs.data]);

  // Follow the newest live run until the reader picks something else.
  useEffect(() => {
    if (selected || ordered.length === 0) return;
    setSelected(ordered[0].run_id);
  }, [ordered, selected]);

  const active = ordered.find((r) => r.run_id === selected) ?? null;
  const liveCount = ordered.filter((r) => r.status === 'running').length;

  return (
    <div className="mx-auto max-w-6xl space-y-5 p-6">
      <header>
        <Eyebrow>Live runs</Eyebrow>
        <h1 className="mt-1 text-2xl font-medium text-ink">What the harness is doing</h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-2">
          {liveCount > 0
            ? `${liveCount} run${liveCount > 1 ? 's' : ''} in progress. Each step appears here as it finishes, so you can watch a run work through a topic rather than wait for the workbook.`
            : 'Nothing running right now. Finished runs stay here with everything they logged, so you can see how one got to its result.'}
        </p>
      </header>

      {runs.isLoading && <Skeleton rows={4} />}
      {runs.isError && (
        <ErrorState title="Could not load runs" detail={(runs.error as Error)?.message} />
      )}

      {runs.data && ordered.length === 0 && (
        <EmptyState title="No runs yet">
          Start one from New run and it will appear here as it works.
        </EmptyState>
      )}

      {ordered.length > 0 && (
        <div className="grid gap-4 lg:grid-cols-[260px_1fr]">
          <Panel className="h-max">
            <PanelHeader title="Runs" hint={liveCount > 0 ? 'Live first' : 'Most recent first'} />
            <ul className="max-h-[70vh] divide-y divide-line overflow-y-auto">
              {ordered.map((run) => {
                const isActive = run.run_id === selected;
                return (
                  <li key={run.run_id}>
                    <button
                      type="button"
                      onClick={() => setSelected(run.run_id)}
                      className={`w-full px-4 py-3 text-left transition-colors ${
                        isActive ? 'bg-sunken' : 'hover:bg-sunken'
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className={`truncate text-sm ${isActive ? 'font-medium text-ink' : 'text-ink-2'}`}>
                          {run.niches.length > 0 ? run.niches.join(', ') : 'untitled'}
                        </span>
                      </div>
                      <div className="mt-1 flex items-center justify-between gap-2">
                        <StatusPill status={run.status} />
                        <span className="font-mono text-[10px] text-ink-3">
                          {elapsed(run.started_at, run.status === 'running' ? null : run.last_activity_at)}
                        </span>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          </Panel>

          {active ? (
            <RunDetail key={active.run_id} run={active} />
          ) : (
            <EmptyState title="Pick a run">Choose one on the left to follow it.</EmptyState>
          )}
        </div>
      )}
    </div>
  );
}
