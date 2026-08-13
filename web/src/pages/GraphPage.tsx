import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from 'd3-force';
import { api, type DiscoveryMethod, type GraphNode } from '../lib/api';
import {
  EmptyState,
  ErrorState,
  Eyebrow,
  Panel,
  PanelHeader,
  Skeleton,
  StatTile,
  TRACK_META,
} from '../components/primitives';
import { compactNumber, decimal } from '../lib/format';

interface SimNode extends SimulationNodeDatum, GraphNode {
  id: string;
  degree: number;
}
type SimLink = SimulationLinkDatum<SimNode> & { edge_type: string };

const WIDTH = 900;
const HEIGHT = 560;

/** Subscriber count spans orders of magnitude; radius is on a log scale so a
 *  1M-sub channel does not swallow the frame. Area, not radius, tracks size. */
function radiusFor(node: SimNode): number {
  const subs = node.subscriber_count ?? 0;
  const base = subs > 0 ? Math.log10(subs + 10) : 1;
  return Math.max(4, Math.min(15, base * 2.1 + node.degree * 0.25));
}

function useForceLayout(nodes: SimNode[], links: SimLink[], reducedMotion: boolean) {
  const [, setTick] = useState(0);
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null);

  useEffect(() => {
    if (nodes.length === 0) return;

    const sim = forceSimulation<SimNode>(nodes)
      .force(
        'link',
        forceLink<SimNode, SimLink>(links)
          .id((d) => d.id)
          .distance(58)
          .strength(0.5),
      )
      .force('charge', forceManyBody<SimNode>().strength(-165))
      .force('center', forceCenter(WIDTH / 2, HEIGHT / 2))
      .force('collide', forceCollide<SimNode>().radius((d) => radiusFor(d) + 4))
      .alphaDecay(0.035);

    simRef.current = sim;

    if (reducedMotion) {
      // Solve to a settled layout without animating a moving frame.
      sim.stop();
      for (let i = 0; i < 220; i += 1) sim.tick();
      setTick((t) => t + 1);
    } else {
      sim.on('tick', () => setTick((t) => t + 1));
    }

    return () => {
      sim.stop();
      sim.on('tick', null);
    };
  }, [nodes, links, reducedMotion]);

  return simRef;
}

function Legend({ counts }: { counts: Record<string, number> }) {
  const order: DiscoveryMethod[] = ['graph_walk', 'both', 'keyword', 'unattributed', 'unhydrated'];
  const present = order.filter((m) => (counts[m] ?? 0) > 0);

  return (
    <ul className="flex flex-wrap gap-x-5 gap-y-2">
      {present.map((method) => {
        const meta = TRACK_META[method];
        return (
          <li key={method} className="flex items-center gap-2">
            <svg width="14" height="14" aria-hidden>
              <circle
                cx="7"
                cy="7"
                r="5"
                fill={meta.filled ? meta.color : 'transparent'}
                stroke={meta.color}
                strokeWidth="1.75"
              />
            </svg>
            <span className="text-xs text-ink-2">{meta.label}</span>
            <span className="tnum font-mono text-xs text-ink-3">{counts[method]}</span>
          </li>
        );
      })}
    </ul>
  );
}

export function GraphPage() {
  const [focus, setFocus] = useState('');
  const [hovered, setHovered] = useState<SimNode | null>(null);

  const reducedMotion = useMemo(
    () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    [],
  );

  const graph = useQuery({
    queryKey: ['graph', focus],
    queryFn: () => api.graph(focus, 400),
  });

  const { nodes, links } = useMemo(() => {
    if (!graph.data) return { nodes: [] as SimNode[], links: [] as SimLink[] };

    const degree = new Map<string, number>();
    for (const e of graph.data.edges) {
      degree.set(e.source_channel_id, (degree.get(e.source_channel_id) ?? 0) + 1);
      degree.set(e.target_channel_id, (degree.get(e.target_channel_id) ?? 0) + 1);
    }

    const simNodes: SimNode[] = graph.data.nodes.map((n) => ({
      ...n,
      id: n.channel_id,
      degree: degree.get(n.channel_id) ?? 0,
    }));

    const present = new Set(simNodes.map((n) => n.id));
    const simLinks: SimLink[] = graph.data.edges
      .filter((e) => present.has(e.source_channel_id) && present.has(e.target_channel_id))
      .map((e) => ({
        source: e.source_channel_id,
        target: e.target_channel_id,
        edge_type: e.edge_type,
      }));

    return { nodes: simNodes, links: simLinks };
  }, [graph.data]);

  useForceLayout(nodes, links, reducedMotion);

  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const n of nodes) out[n.discovery_method] = (out[n.discovery_method] ?? 0) + 1;
    return out;
  }, [nodes]);

  const exclusive = counts.graph_walk ?? 0;
  const share = nodes.length > 0 ? (exclusive / nodes.length) * 100 : 0;

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <header className="mb-6">
        <Eyebrow>Console</Eyebrow>
        <h1 className="mt-1 font-display text-2xl font-semibold tracking-tight text-balance text-ink">
          Discovery graph
        </h1>
        <p className="mt-1.5 max-w-2xl text-sm text-ink-2">
          Every channel in the store, drawn by the relationships the crawl followed. Filled marks
          are channels the keyword track never returned — the ones this project exists to find.
        </p>
      </header>

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile label="Channels drawn" value={compactNumber(nodes.length)} />
        <StatTile
          label="Graph-walk only"
          value={compactNumber(exclusive)}
          accent="var(--track-graph)"
          sub="Invisible to keyword search"
        />
        <StatTile label="Share of graph" value={`${decimal(share, 1)}%`} />
        <StatTile label="Edges" value={compactNumber(links.length)} />
      </div>

      <Panel>
        <PanelHeader
          title="Frontier"
          hint="Drag-free force layout; hover a channel for detail."
          right={
            <input
              value={focus}
              onChange={(e) => setFocus(e.target.value)}
              placeholder="Focus a channel_id…"
              spellCheck={false}
              aria-label="Focus the graph on one channel"
              name="graph-focus"
              autoComplete="off"
              className="w-52 rounded-md border border-line bg-page px-2.5 py-1.5 font-mono text-xs text-ink placeholder:text-ink-3"
            />
          }
        />

        {graph.isLoading ? (
          <Skeleton rows={5} />
        ) : graph.isError ? (
          <div className="p-4">
            <ErrorState
              title="Could not load the graph"
              detail={graph.error instanceof Error ? graph.error.message : undefined}
            />
          </div>
        ) : nodes.length === 0 ? (
          <EmptyState title="No discovery edges yet">
            The graph fills in once a run’s graph_walk track records relationships.
          </EmptyState>
        ) : (
          <>
            <div className="relative">
              <svg
                viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
                className="w-full"
                style={{ height: HEIGHT, background: 'var(--surface)' }}
                role="img"
                aria-label={`Discovery graph: ${nodes.length} channels, ${links.length} relationships, ${exclusive} reachable only by the graph walk`}
              >
                <g>
                  {links.map((link, i) => {
                    const s = link.source as SimNode;
                    const t = link.target as SimNode;
                    if (typeof s !== 'object' || typeof t !== 'object') return null;
                    const touched =
                      hovered && (s.id === hovered.id || t.id === hovered.id);
                    return (
                      <line
                        key={i}
                        x1={s.x}
                        y1={s.y}
                        x2={t.x}
                        y2={t.y}
                        stroke={touched ? 'var(--track-graph)' : 'var(--line)'}
                        strokeWidth={touched ? 1.6 : 1}
                        opacity={hovered && !touched ? 0.25 : 0.8}
                      />
                    );
                  })}
                </g>

                <g>
                  {nodes.map((node) => {
                    const meta = TRACK_META[node.discovery_method] ?? TRACK_META.unattributed;
                    const r = radiusFor(node);
                    const dim = hovered && hovered.id !== node.id;
                    return (
                      <circle
                        key={node.id}
                        cx={node.x}
                        cy={node.y}
                        r={r}
                        fill={meta.filled ? meta.color : 'var(--surface)'}
                        stroke={meta.color}
                        strokeWidth="1.75"
                        opacity={dim ? 0.45 : 1}
                        tabIndex={0}
                        role="button"
                        aria-label={`${node.title ?? node.channel_id}, ${meta.label}`}
                        onMouseEnter={() => setHovered(node)}
                        onMouseLeave={() => setHovered(null)}
                        onFocus={() => setHovered(node)}
                        onBlur={() => setHovered(null)}
                        style={{ cursor: 'pointer' }}
                      />
                    );
                  })}
                </g>
              </svg>

              {hovered ? (
                <div
                  className="pointer-events-none absolute top-3 left-3 max-w-xs rounded-md border border-line bg-raised px-3 py-2"
                  style={{ boxShadow: 'var(--shadow)' }}
                  role="status"
                >
                  <p className="truncate text-sm font-medium text-ink">
                    {hovered.title ?? '(not hydrated)'}
                  </p>
                  <p className="mt-0.5 font-mono text-[11px] text-ink-3">{hovered.channel_id}</p>
                  <dl className="mt-2 space-y-0.5 text-[11px]">
                    <div className="flex justify-between gap-4">
                      <dt className="text-ink-3">Track</dt>
                      <dd className="text-ink-2">
                        {(TRACK_META[hovered.discovery_method] ?? TRACK_META.unattributed).label}
                      </dd>
                    </div>
                    <div className="flex justify-between gap-4">
                      <dt className="text-ink-3">Subscribers</dt>
                      <dd className="tnum font-mono text-ink-2">
                        {compactNumber(hovered.subscriber_count)}
                      </dd>
                    </div>
                    <div className="flex justify-between gap-4">
                      <dt className="text-ink-3">Peak outlier</dt>
                      <dd className="tnum font-mono text-ink-2">
                        {hovered.max_outlier_score ? `${decimal(hovered.max_outlier_score)}×` : '—'}
                      </dd>
                    </div>
                    <div className="flex justify-between gap-4">
                      <dt className="text-ink-3">Links</dt>
                      <dd className="tnum font-mono text-ink-2">{hovered.degree}</dd>
                    </div>
                  </dl>
                  <p className="mt-2 border-t border-line pt-1.5 text-[11px] text-ink-3">
                    {(TRACK_META[hovered.discovery_method] ?? TRACK_META.unattributed).note}
                  </p>
                </div>
              ) : null}
            </div>

            <div className="border-t border-line px-4 py-3">
              <Legend counts={counts} />
            </div>
          </>
        )}
      </Panel>
    </div>
  );
}
