import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type NodeLogEntry, type Run } from '../lib/api';
import {
  computeProgress,
  phaseOf,
  PHASE_DETAIL,
  PHASE_LABEL,
  PHASE_ORDER,
  type Phase,
} from '../lib/progress';
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
  ConfirmDialog,
  DangerButton,
} from '../components/primitives';

/* --------------------------------------------------------------------------
 * A run is a chain of graph nodes, each logged as it completes. The stream is
 * the monitor: nothing else says what the harness is doing *right now*.
 * ----------------------------------------------------------------------- */

/** What each node is called, in the client's terms rather than the
 *  graph's. The PHASE each belongs to lives in lib/progress.ts, which the
 *  progress bar reads too -- one node/phase map, not two that can drift. */
const NODE_LABEL: Record<string, string> = {
  scan_niches: 'Reading the topic',
  expand_niche_adjacency: 'Looking for adjacent areas',
  build_taxonomy: 'Mapping sub-niches',
  select_next_node: 'Choosing the next branch',
  underperformer_discovery: 'Sweeping smaller channels',
  graph_walk: 'Following channel references',
  keyword_search: 'Searching keywords',
  breakout_scanner: 'Scanning for breakouts',
  new_channel_discovery: 'Finding new channels',
  hydrate_metadata: 'Pulling channel details',
  resolve_geo_language: 'Resolving region and language',
  extract_metadata_signals: 'Reading metadata signals',
  score_signals: 'Scoring signals',
  classify_channel: 'Classifying channels',
  resolve_first_video_date: 'Dating first uploads',
  check_saturation: 'Checking for saturation',
  cluster_branch: 'Clustering the branch',
  compact_branch: 'Summarising the branch',
  extract_success_failure_factors: 'Extracting success factors',
  describe_video_titles: 'Describing videos',
  populate_taxonomy_dimensions: 'Filling taxonomy dimensions',
  // Runs on every topic but only ever fills rows whose niche is under the
  // crime category, so on a technology run it appears, finds nothing, and
  // passes. Naming the category keeps that from reading as a mistake.
  populate_crime_metadata: 'Filling crime case fields',
  populate_shared_fields: 'Filling shared fields',
  assign_cohorts: 'Assigning cohorts',
  finalize_dataset: 'Finalising the dataset',
  synthesize: 'Writing the report',
};

const PHASE_COLOR: Record<Phase, string> = {
  plan: 'var(--track-seed)',
  discover: 'var(--track-keyword)',
  enrich: 'var(--track-graph)',
  assess: 'var(--grade-moderate)',
  finish: 'var(--status-good)',
};

function nodeMeta(name: string) {
  return {
    label: NODE_LABEL[name] ?? name.replace(/_/g, ' '),
    phase: phaseOf(name),
  };
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

function ActivityStream({
  isLive,
  entries,
  connection,
}: {
  isLive: boolean;
  entries: NodeLogEntry[];
  connection: string;
}) {
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

/* ---- progress ---------------------------------------------------------- */

function ProgressPanel({
  progress,
  isLive,
  status,
}: {
  progress: ReturnType<typeof computeProgress>;
  isLive: boolean;
  status: string;
}) {
  const stalled = status === 'stopped' && progress.percent < 100;

  return (
    <Panel>
      <div className="p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <span className="text-sm font-medium text-ink">
            {progress.percent === 100
              ? 'Finished'
              : PHASE_LABEL[progress.phase]}
          </span>
          <span className="font-mono text-sm tabular-nums text-ink-2">
            {progress.percent}%
          </span>
        </div>

        <div
          className="mt-2 h-2 overflow-hidden rounded-full bg-sunken"
          role="progressbar"
          aria-valuenow={progress.percent}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Run progress"
        >
          <div
            className="h-full rounded-full transition-[width] duration-700 ease-out"
            style={{
              width: `${Math.max(progress.percent, 2)}%`,
              background: stalled
                ? 'var(--status-critical)'
                : progress.percent === 100
                  ? 'var(--status-good)'
                  : 'var(--focus)',
              // A live run's bar breathes, so a long phase does not read as
              // a frozen page. Removed the moment it stops being live.
              animation: isLive ? 'pulse 2.4s ease-in-out infinite' : undefined,
            }}
          />
        </div>

        <p className="mt-2 text-xs leading-snug text-ink-3">
          {progress.percent === 100
            ? 'Nothing left to do.'
            : PHASE_DETAIL[progress.phase]}
        </p>

        {/* What happens after this. The client's own question -- "what is it
            going to do next" -- which the raw node stream answers only if
            you already know the graph. */}
        <ol className="mt-3 flex flex-wrap gap-x-3 gap-y-1.5">
          {PHASE_ORDER.map((p: Phase) => {
            const isDone = progress.done.includes(p);
            const isNow = progress.phase === p && progress.percent < 100;
            return (
              <li
                key={p}
                className={`flex items-center gap-1.5 text-xs ${
                  isNow ? 'font-medium text-ink' : isDone ? 'text-ink-2' : 'text-ink-3'
                }`}
              >
                <span
                  aria-hidden
                  className="size-1.5 shrink-0 rounded-full"
                  style={{
                    background: isDone
                      ? 'var(--status-good)'
                      : isNow
                        ? 'var(--focus)'
                        : 'var(--line)',
                  }}
                />
                {PHASE_LABEL[p]}
              </li>
            );
          })}
        </ol>

        {stalled && (
          <p className="mt-3 rounded border border-line bg-sunken px-3 py-2 text-xs leading-snug text-ink-2">
            This run stopped before finishing, so it did not reach the export.
            Everything it had already collected is kept.
          </p>
        )}
      </div>
    </Panel>
  );
}

/* ---- the selected run ------------------------------------------------- */

function RunDetail({ run }: { run: Run }) {
  const isLive = run.status === 'running';
  // Owned here rather than inside ActivityStream: the progress bar and the
  // stream are two readings of the same log, and opening two SSE
  // connections to say the same thing would be wasteful and could disagree.
  const { entries, connection } = useRunEvents(run.run_id, isLive);
  const progress = useMemo(
    () => computeProgress(entries, run.status),
    [entries, run.status],
  );

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

  const queryClient = useQueryClient();
  const [askStop, setAskStop] = useState(false);
  const stop = useMutation({
    mutationFn: () => api.stopRun(run.run_id),
    onSuccess: () => {
      setAskStop(false);
      queryClient.invalidateQueries({ queryKey: ['runs'] });
    },
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <StatusPill status={run.status} />
          <span className="text-sm text-ink-2">
            {run.niches.length > 0 ? run.niches.join(', ') : 'untitled'}
            {run.depth_label ? ` · ${run.depth_label}` : ''}
          </span>
        </div>
        {isLive && (
          <DangerButton
            onClick={() => setAskStop(true)}
            title="Ends the run. Everything already collected is kept."
          >
            Stop run
          </DangerButton>
        )}
      </div>

      {stop.isError && (
        <p className="text-xs text-[var(--status-critical)]">
          {(stop.error as Error).message}
        </p>
      )}
      {stop.isSuccess && !isLive && (
        <p className="text-xs text-ink-3">
          Stopped. It kept everything it had finished; the workbook was not
          exported, since that is the last step.
        </p>
      )}

      <ProgressPanel progress={progress} isLive={isLive} status={run.status} />

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
        <ActivityStream isLive={isLive} entries={entries} connection={connection} />
      </Panel>

      <ConfirmDialog
        open={askStop}
        title="Stop this run?"
        body={
          <>
            <p>
              <span className="font-medium text-ink">
                {run.niches.join(', ') || run.run_id}
              </span>{' '}
              will stop where it is. Everything it has already collected —
              channels, videos and any analysis finished so far — is kept.
            </p>
            <p className="mt-2">
              It will not produce a workbook, since the export is the last
              step. You can export one later from the run&rsquo;s data.
            </p>
          </>
        }
        confirmLabel="Stop run"
        pending={stop.isPending}
        pendingLabel="Stopping…"
        onConfirm={() => stop.mutate()}
        onCancel={() => setAskStop(false)}
      />
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

  const queryClient = useQueryClient();
  const [pendingDelete, setPendingDelete] = useState<Run | null>(null);
  const remove = useMutation({
    mutationFn: (runId: string) => api.deleteRun(runId),
    onSuccess: (_data, runId) => {
      setPendingDelete(null);
      // Drop the selection if it was the row just removed, or the detail
      // pane keeps rendering a run that no longer exists.
      setSelected((current) => (current === runId ? null : current));
      queryClient.invalidateQueries({ queryKey: ['runs'] });
    },
  });

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

      {remove.isError && (
        <ErrorState
          title="Could not delete that run"
          detail={(remove.error as Error).message}
        />
      )}

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
                    {/* Outside the select button: a delete nested inside it
                        would also select the row it is about to remove. */}
                    {run.status !== 'running' && (
                      <div className="px-4 pb-3">
                        <DangerButton
                          onClick={() => setPendingDelete(run)}
                          title="Removes this run from the console. The channels it found stay in the database."
                        >
                          Delete
                        </DangerButton>
                      </div>
                    )}
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

      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete this run?"
        body={
          <>
            <p>
              <span className="font-medium text-ink">
                {pendingDelete?.niches.join(', ') || pendingDelete?.run_id}
              </span>{' '}
              will be removed from the console, along with its activity log
              and spend ledger.
            </p>
            <p className="mt-2">
              The channels and videos it discovered stay in the database —
              other workbooks cite them — so this removes the record of the
              run, not the research it did.
            </p>
          </>
        }
        confirmLabel="Delete run"
        pending={remove.isPending}
        onConfirm={() => pendingDelete && remove.mutate(pendingDelete.run_id)}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
