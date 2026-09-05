import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type SimulationNodeDatum,
} from 'd3-force';
import { api, type DiscoveryMethod, type WorkbookGraphNode } from '../lib/api';
import { WorkbookPicker } from '../components/WorkbookPicker';
import {
  Panel,
  PanelHeader,
  Eyebrow,
  ErrorState,
  Skeleton,
  TRACK_META,
} from '../components/primitives';

interface SimNode extends SimulationNodeDatum {
  id: string;
  title: string;
  method: DiscoveryMethod;
  subs: number;
  subNiche: string | null;
}

function trackOf(node: WorkbookGraphNode): DiscoveryMethod {
  const m = (node.discovery_method ?? 'unattributed') as DiscoveryMethod;
  return m in TRACK_META ? m : ('unattributed' as DiscoveryMethod);
}

/** Radius by subscriber count, on a log scale so a 20M channel doesn't eat the canvas. */
function radiusFor(subs: number): number {
  if (subs <= 0) return 3;
  return Math.max(3, Math.min(14, Math.log10(subs) * 2.2));
}

export function GraphPage() {
  const [workbookId, setWorkbookId] = useState<string | null>(null);
  const [hovered, setHovered] = useState<SimNode | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  const workbooks = useQuery({ queryKey: ['workbooks'], queryFn: api.workbooks });
  const active = workbookId ?? workbooks.data?.find((w) => w.available)?.id ?? null;

  const graph = useQuery({
    queryKey: ['workbook-graph', active],
    queryFn: () => api.workbookGraph(active as string),
    enabled: !!active,
  });

  const nodes = useMemo<SimNode[]>(
    () =>
      (graph.data?.nodes ?? []).map((n) => ({
        id: n.channel_id,
        title: n.title ?? n.channel_id,
        method: trackOf(n),
        subs: n.subscriber_count ?? 0,
        subNiche: n.sub_niche,
      })),
    [graph.data],
  );

  // Only edges wholly inside the workbook can be drawn — an edge to a channel
  // the trim dropped has no node to attach to on this canvas.
  const links = useMemo(
    () =>
      (graph.data?.edges ?? [])
        .filter((e) => e.internal)
        .map((e) => ({ source: e.source, target: e.target })),
    [graph.data],
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || nodes.length === 0) return;

    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.scale(dpr, dpr);

    const sim = forceSimulation(nodes)
      .force('charge', forceManyBody().strength(-24))
      .force('center', forceCenter(width / 2, height / 2))
      .force('collide', forceCollide<SimNode>().radius((d) => radiusFor(d.subs) + 2))
      .force(
        'link',
        forceLink(links as never)
          .id((d) => (d as SimNode).id)
          .distance(40)
          .strength(0.4),
      );

    function draw() {
      if (!ctx) return;
      ctx.clearRect(0, 0, width, height);

      ctx.strokeStyle = 'rgba(125,145,165,0.28)';
      ctx.lineWidth = 1;
      for (const link of links as unknown as { source: SimNode; target: SimNode }[]) {
        if (typeof link.source === 'string') continue;
        ctx.beginPath();
        ctx.moveTo(link.source.x ?? 0, link.source.y ?? 0);
        ctx.lineTo(link.target.x ?? 0, link.target.y ?? 0);
        ctx.stroke();
      }

      for (const node of nodes) {
        const meta = TRACK_META[node.method] ?? TRACK_META.unattributed;
        ctx.beginPath();
        ctx.arc(node.x ?? 0, node.y ?? 0, radiusFor(node.subs), 0, Math.PI * 2);
        ctx.fillStyle = meta.filled ? meta.color : 'transparent';
        ctx.strokeStyle = meta.color;
        ctx.lineWidth = 1.5;
        if (meta.filled) ctx.fill();
        ctx.stroke();
      }
    }

    sim.on('tick', draw);

    function onMove(event: MouseEvent) {
      const rect = canvas!.getBoundingClientRect();
      const mx = event.clientX - rect.left;
      const my = event.clientY - rect.top;
      let found: SimNode | null = null;
      for (const node of nodes) {
        const dx = (node.x ?? 0) - mx;
        const dy = (node.y ?? 0) - my;
        if (Math.hypot(dx, dy) <= radiusFor(node.subs) + 4) {
          found = node;
          break;
        }
      }
      setHovered(found);
    }
    canvas.addEventListener('mousemove', onMove);

    return () => {
      sim.stop();
      canvas.removeEventListener('mousemove', onMove);
    };
  }, [nodes, links]);

  const tracks = graph.data?.by_track ?? {};

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-6">
      <header>
        <Eyebrow>Discovery graph</Eyebrow>
        <h1 className="mt-1 text-2xl font-medium text-ink">
          How this workbook&rsquo;s channels were reached
        </h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-2">
          Every channel that shipped in the selected workbook, coloured by the
          track that found it. Size is subscriber count on a log scale.
        </p>
      </header>

      <WorkbookPicker value={active} onChange={setWorkbookId} />

      {graph.isLoading && <Skeleton rows={5} />}
      {graph.isError && (
        <ErrorState
          title="Could not load the graph"
          detail={(graph.error as Error)?.message}
        />
      )}

      {graph.data && (
        <>
          <div className="flex flex-wrap gap-4">
            {Object.entries(tracks).map(([method, count]) => {
              const meta =
                TRACK_META[method as DiscoveryMethod] ?? TRACK_META.unattributed;
              return (
                <div key={method} className="flex items-center gap-2 text-sm">
                  <span
                    aria-hidden
                    className="size-2.5 rounded-full border-2"
                    style={{
                      borderColor: meta.color,
                      background: meta.filled ? meta.color : 'transparent',
                    }}
                  />
                  <span className="text-ink-2">{meta.label}</span>
                  <span className="tabular-nums text-ink-3">{count}</span>
                </div>
              );
            })}
          </div>

          <Panel>
            <PanelHeader
              title={`${graph.data.channel_count} channels`}
              hint={`${graph.data.internal_edge_count} links inside the workbook`}
            />
            <div className="relative">
              <canvas
                ref={canvasRef}
                className="h-[460px] w-full"
                aria-label="Discovery graph of the workbook's channels"
              />
              {hovered && (
                <div className="pointer-events-none absolute left-3 top-3 max-w-xs rounded-md border border-line bg-raised px-3 py-2 shadow-[var(--shadow)]">
                  <p className="text-sm font-medium text-ink">{hovered.title}</p>
                  <p className="mt-0.5 text-xs text-ink-2">
                    {hovered.subs.toLocaleString()} subscribers
                  </p>
                  {hovered.subNiche && (
                    <p className="text-xs text-ink-3">{hovered.subNiche}</p>
                  )}
                  <p className="mt-1 text-xs text-ink-3">
                    {(TRACK_META[hovered.method] ?? TRACK_META.unattributed).label}
                  </p>
                </div>
              )}
            </div>
          </Panel>

          {graph.data.internal_edge_count === 0 && (
            <p className="rounded border border-line bg-sunken px-3 py-2 text-xs leading-snug text-ink-2">
              No links are drawn because every channel here was reached
              independently by keyword search rather than by walking from
              another channel in the set. That is a real property of this
              workbook, not a missing dataset.
            </p>
          )}
        </>
      )}
    </div>
  );
}
