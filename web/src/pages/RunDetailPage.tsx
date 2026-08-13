import { useMemo } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api, type NodeLogEntry, type RunState, type TreeNode } from '../lib/api';
import { useRunEvents } from '../hooks/useRunEvents';
import {
  EmptyState,
  ErrorState,
  Panel,
  PanelHeader,
  Skeleton,
  StatTile,
  StatusPill,
} from '../components/primitives';
import { NoveltyDecay, type NoveltySeries } from '../components/charts';
import { ReportView } from './ReportView';
import { clockTime, compactNumber, ms, usd } from '../lib/format';

const NODE_STATUS: Record<TreeNode['status'], { label: string; color: string }> = {
  pending: { label: 'Pending', color: 'var(--ink-3)' },
  active: { label: 'Active', color: 'var(--track-graph)' },
  saturated: { label: 'Saturated', color: 'var(--track-seed)' },
  compacted: { label: 'Compacted', color: 'var(--status-good)' },
};

function TaxonomyTree({ tree, activeId }: { tree: Record<string, TreeNode>; activeId: string | null }) {
  const nodes = useMemo(
    () =>
      Object.values(tree).sort((a, b) => a.depth - b.depth || a.id.localeCompare(b.id)),
    [tree],
  );

  if (nodes.length === 0) {
    return <EmptyState title="No taxonomy yet">build_taxonomy has not run for this thread.</EmptyState>;
  }

  return (
    <ul className="divide-y divide-line">
      {nodes.map((node) => {
        const meta = NODE_STATUS[node.status] ?? NODE_STATUS.pending;
        const isActive = node.id === activeId;
        return (
          <li
            key={node.id}
            className="px-4 py-2.5"
            style={{
              paddingLeft: 16 + node.depth * 18,
              background: isActive ? 'color-mix(in oklab, var(--track-graph) 7%, transparent)' : undefined,
            }}
          >
            <div className="flex items-center gap-2">
              <span
                aria-hidden
                className="h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ background: meta.color }}
              />
              <span className="truncate text-sm text-ink">{node.label || node.id}</span>
              <span className="ml-auto shrink-0 font-mono text-[10px] tracking-wide text-ink-3 uppercase">
                {meta.label}
              </span>
            </div>
            {node.keywords.length > 0 ? (
              <p className="mt-1 truncate font-mono text-[11px] text-ink-3">
                {node.keywords.slice(0, 5).join(' · ')}
              </p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

function LogStream({ entries, connection }: { entries: NodeLogEntry[]; connection: string }) {
  if (entries.length === 0) {
    return <EmptyState title="No steps recorded yet">Telemetry appears as each node completes.</EmptyState>;
  }

  return (
    <ol className="max-h-[420px] divide-y divide-line overflow-y-auto">
      {[...entries].reverse().map((entry, i) => (
        <li
          key={`${entry.timestamp}-${i}`}
          className={i === 0 && connection === 'live' ? 'animate-fade-rise px-4 py-2.5' : 'px-4 py-2.5'}
        >
          <div className="flex items-baseline gap-2">
            <span className="tnum font-mono text-[11px] text-ink-3">{clockTime(entry.timestamp)}</span>
            <span className="font-mono text-xs font-medium text-ink">{entry.node_name}</span>
            <span className="tnum ml-auto font-mono text-[11px] text-ink-3">
              {entry.latency_ms !== null ? ms(entry.latency_ms) : ''}
              {entry.cost_usd ? ` · ${usd(entry.cost_usd)}` : ''}
            </span>
          </div>
          {Object.keys(entry.input_summary ?? {}).length > 0 ? (
            <p className="mt-1 font-mono text-[11px] break-words text-ink-3">
              {Object.entries(entry.input_summary)
                .map(([k, v]) => `${k}=${String(v)}`)
                .join('  ')}
            </p>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function ProgressTab({ state, runId, live }: { state: RunState | null; runId: string; live: boolean }) {
  const { entries, connection } = useRunEvents(runId, live);

  const noveltySeries = useMemo<NoveltySeries[]>(() => {
    if (!state) return [];
    const active = state.active_node_id ? state.tree[state.active_node_id] : null;
    const node = active ?? Object.values(state.tree)[0];
    if (!node) return [];
    return [
      {
        key: 'graph',
        label: 'Graph walk',
        color: 'var(--track-graph)',
        values: node._gw_novelty_history ?? [],
      },
      {
        key: 'keyword',
        label: 'Keyword',
        color: 'var(--track-keyword)',
        values: node._kw_novelty_history ?? [],
      },
    ];
  }, [state]);

  return (
    <div className="space-y-5">
      {state ? (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile label="Spent" value={usd(state.budget_spent_usd)} />
          <StatTile label="Channels" value={compactNumber(state.discovered_channel_count)} />
          <StatTile
            label="Graph-walk only"
            value={compactNumber(state.graph_walk_exclusive_count)}
            accent="var(--track-graph)"
            sub="Keyword search never returned these"
          />
          <StatTile label="Videos" value={compactNumber(state.discovered_video_count)} />
        </div>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel>
          <PanelHeader
            title="Novelty decay"
            hint="Both tracks falling below the threshold is what ends a branch."
          />
          <div className="px-4 py-4">
            <NoveltyDecay series={noveltySeries} threshold={0.05} />
            <p className="mt-3 text-xs text-ink-3">
              Each round, novelty rate is the share of channels a track hadn't already seen. A
              branch saturates once both keyword search and graph walk stay under 5% novelty for
              3 rounds in a row — or run out of new queries and unexpanded channels outright. The
              run itself ends only once every branch has saturated independently, not on a timer
              or a spend target.
            </p>
          </div>
        </Panel>

        <Panel>
          <PanelHeader title="Taxonomy" hint="Branches the run is working through." />
          {state ? (
            <TaxonomyTree tree={state.tree} activeId={state.active_node_id} />
          ) : (
            <EmptyState title="No checkpoint state" />
          )}
        </Panel>
      </div>

      <Panel>
        <PanelHeader
          title="Steps"
          hint="Streamed from the run’s log as each node lands."
          right={
            <span className="font-mono text-[10px] text-ink-3">
              {connection === 'live' ? 'live' : connection === 'ended' ? 'ended' : connection}
            </span>
          }
        />
        <LogStream entries={entries} connection={connection} />
      </Panel>

      {state && state.errors.length > 0 ? (
        <Panel>
          <PanelHeader title={`Errors (${state.errors.length})`} />
          <ul className="divide-y divide-line">
            {state.errors.map((err, i) => (
              <li key={i} className="px-4 py-2.5">
                <p className="font-mono text-xs text-ink">
                  {err.node_name} · {err.error_type}
                </p>
                <p className="mt-0.5 font-mono text-[11px] break-words text-ink-3">{err.message}</p>
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}
    </div>
  );
}

export function RunDetailPage() {
  const { runId = '' } = useParams();

  // Tab lives in the URL so a finished report can be linked to directly,
  // and a refresh does not dump you back on Progress.
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get('view') === 'report' ? 'report' : 'progress';
  const setTab = (next: 'progress' | 'report') =>
    setSearchParams(next === 'report' ? { view: 'report' } : {}, { replace: true });

  const detail = useQuery({
    queryKey: ['run', runId],
    queryFn: () => api.run(runId),
    refetchInterval: (query) => (query.state.data?.run.status === 'running' ? 5_000 : false),
  });

  if (detail.isLoading) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-8">
        <Skeleton rows={6} />
      </div>
    );
  }

  if (detail.isError || !detail.data) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-8">
        <ErrorState
          title="Could not load this run"
          detail={detail.error instanceof Error ? detail.error.message : undefined}
        />
      </div>
    );
  }

  const { run, state, state_error } = detail.data;
  const report = state?.final_report ?? null;

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <header className="mb-6">
        <Link to="/runs" className="font-mono text-[11px] text-ink-3 hover:text-ink">
          ← Runs
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="font-display text-2xl font-semibold tracking-tight text-balance text-ink">
            {state?.selected_niche || run.niches.join(', ') || run.run_id}
          </h1>
          <StatusPill status={run.status} />
        </div>
        <p className="mt-1 font-mono text-[11px] text-ink-3">
          {run.run_id} · thread {run.thread_id || '—'}
        </p>
      </header>

      {state_error ? (
        <div className="mb-5">
          <ErrorState title="Checkpoint state unavailable" detail={state_error} />
        </div>
      ) : null}

      <div
        className="mb-5 flex gap-1 border-b border-line"
        role="tablist"
        aria-label="Run views"
      >
        {(
          [
            ['progress', 'Progress'],
            ['report', report ? 'Report' : 'Report (pending)'],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm transition-colors ${
              tab === key
                ? 'border-current font-medium text-ink'
                : 'border-transparent text-ink-3 hover:text-ink-2'
            }`}
            style={tab === key ? { borderColor: 'var(--track-graph)' } : undefined}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'progress' ? (
        <ProgressTab state={state} runId={run.run_id} live={run.status === 'running'} />
      ) : report ? (
        <ReportView report={report} />
      ) : (
        <Panel>
          <EmptyState title="No report yet">
            synthesize writes the graded report once every branch has saturated.
          </EmptyState>
        </Panel>
      )}
    </div>
  );
}
