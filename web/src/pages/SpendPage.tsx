import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import {
  ErrorState,
  Eyebrow,
  Panel,
  PanelHeader,
  Skeleton,
  StatTile,
} from '../components/primitives';
import { BarList } from '../components/charts';
import { ms, usd } from '../lib/format';

export function SpendPage() {
  const costs = useQuery({ queryKey: ['costs'], queryFn: api.costs, refetchInterval: 15_000 });

  if (costs.isLoading) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-8">
        <Skeleton rows={5} />
      </div>
    );
  }

  if (costs.isError || !costs.data) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-8">
        <ErrorState
          title="Could not load spend"
          detail={costs.error instanceof Error ? costs.error.message : undefined}
        />
      </div>
    );
  }

  const { total_usd, by_node, by_run } = costs.data;
  const paidNodes = by_node.filter((n) => n.cost_usd > 0);
  const slowest = [...by_node]
    .filter((n) => n.avg_latency_ms !== null)
    .sort((a, b) => (b.avg_latency_ms ?? 0) - (a.avg_latency_ms ?? 0))
    .slice(0, 8);

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <header className="mb-6">
        <Eyebrow>Console</Eyebrow>
        <h1 className="mt-1 font-display text-2xl font-semibold tracking-tight text-balance text-ink">Spend</h1>
        <p className="mt-1.5 max-w-2xl text-sm text-ink-2">
          Model spend recorded per node across every run on this machine. Retry attempts that
          failed are not counted — only the call that succeeded.
        </p>
      </header>

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile label="Total spend" value={usd(total_usd)} />
        <StatTile label="Runs" value={by_run.length} />
        <StatTile label="Node executions" value={by_node.reduce((sum, n) => sum + n.calls, 0)} />
        <StatTile label="Paid nodes" value={paidNodes.length} sub="The rest are deterministic" />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel>
          <PanelHeader title="Spend by node" hint="Only LLM-touching nodes cost anything." />
          <BarList
            rows={paidNodes.map((n) => ({
              key: n.node_name,
              label: n.node_name,
              value: n.cost_usd,
              display: usd(n.cost_usd),
              note: `${n.calls} call${n.calls === 1 ? '' : 's'}`,
            }))}
            emptyLabel="No model spend recorded yet."
          />
        </Panel>

        <Panel>
          <PanelHeader title="Slowest nodes" hint="Mean latency per execution." />
          <BarList
            rows={slowest.map((n) => ({
              key: n.node_name,
              label: n.node_name,
              value: n.avg_latency_ms ?? 0,
              display: ms(n.avg_latency_ms),
              color: 'var(--track-seed)',
              note: `${n.calls} call${n.calls === 1 ? '' : 's'}`,
            }))}
            emptyLabel="No latency recorded yet."
          />
        </Panel>
      </div>

      <div className="mt-5">
        <Panel>
          <PanelHeader title="Spend by run" />
          <BarList
            rows={by_run.map((r) => ({
              key: r.run_id,
              label: r.niches.length > 0 ? r.niches.join(', ') : r.run_id,
              value: r.cost_usd,
              display: usd(r.cost_usd),
              note: r.run_id,
            }))}
            emptyLabel="No runs recorded yet."
          />
        </Panel>
      </div>
    </div>
  );
}
