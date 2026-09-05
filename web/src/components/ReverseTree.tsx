import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { hierarchy, tree as d3tree, type HierarchyPointNode } from 'd3-hierarchy';
import { select } from 'd3-selection';
import { zoom as d3zoom, zoomIdentity, type ZoomBehavior } from 'd3-zoom';
// Imported for the side effect: d3-transition augments Selection with
// .transition(), which the zoom buttons animate through.
import 'd3-transition';
import type { TreeNodeData } from '../lib/api';
import { TRACK_META } from './primitives';
import type { DiscoveryMethod } from '../lib/api';

/**
 * The workbook drawn as a reverse tree: the root sits at the BOTTOM and
 * growth runs upward, so channels fan out overhead the way a real tree's
 * canopy does. Reading it bottom-up answers "what is this deliverable made
 * of"; reading it top-down answers "where did this channel come from".
 *
 * A force-directed layout was the wrong tool here -- 236 of Crime's 240
 * channels have no edge to any other channel in the set, so that view was
 * 240 unconnected dots. The hierarchy is the structure that actually
 * exists.
 */

const NODE_WIDTH = 190;
const NODE_HEIGHT = 78;

interface Props {
  data: TreeNodeData;
  height?: number;
}

function isBranch(node: TreeNodeData): boolean {
  return !!node.children && node.children.length > 0;
}

function nodeFill(node: TreeNodeData): string {
  if (node.kind === 'channel') {
    const method = (node.discovery_method ?? 'unattributed') as DiscoveryMethod;
    return (TRACK_META[method] ?? TRACK_META.unattributed).color;
  }
  if (node.kind === 'root') return 'var(--focus)';
  if (node.kind === 'family') return 'var(--track-seed)';
  return 'var(--ink-3)';
}

export function ReverseTree({ data, height = 560 }: Props) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const gRef = useRef<SVGGElement | null>(null);
  const zoomRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);

  // Collapsed by id. Sub-niches start collapsed: expanding all of Finance
  // at once is 350 leaves, which is a wall rather than a picture.
  const [collapsed, setCollapsed] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    for (const family of data.children ?? []) {
      for (const sub of family.children ?? []) initial.add(sub.id);
    }
    return initial;
  });
  const [hovered, setHovered] = useState<TreeNodeData | null>(null);

  // Rebuild the initial collapse set when the workbook changes.
  useEffect(() => {
    const initial = new Set<string>();
    for (const family of data.children ?? []) {
      for (const sub of family.children ?? []) initial.add(sub.id);
    }
    setCollapsed(initial);
  }, [data]);

  const layout = useMemo(() => {
    const root = hierarchy<TreeNodeData>(data, (node) =>
      collapsed.has(node.id) ? undefined : node.children,
    );
    const treeLayout = d3tree<TreeNodeData>().nodeSize([NODE_WIDTH, NODE_HEIGHT]);
    return treeLayout(root);
  }, [data, collapsed]);

  const nodes = layout.descendants();
  const links = layout.links();

  // Depth runs downward from d3; flip it so the root lands at the bottom.
  const maxDepth = Math.max(...nodes.map((n) => n.y), 1);
  const flipY = useCallback((y: number) => maxDepth - y, [maxDepth]);

  const bounds = useMemo(() => {
    const xs = nodes.map((n) => n.x);
    return { minX: Math.min(...xs), maxX: Math.max(...xs) };
  }, [nodes]);

  // Wire zoom once, then re-centre whenever the tree's extent changes.
  useEffect(() => {
    const svg = svgRef.current;
    const g = gRef.current;
    if (!svg || !g) return;

    const behavior = d3zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.15, 2.5])
      .on('zoom', (event) => {
        g.setAttribute('transform', event.transform.toString());
      });
    zoomRef.current = behavior;
    select(svg).call(behavior);

    const width = svg.clientWidth || 800;
    const centre = (bounds.minX + bounds.maxX) / 2;
    const initial = zoomIdentity
      .translate(width / 2 - centre * 0.7, height - 90)
      .scale(0.7);
    select(svg).call(behavior.transform, initial);

    return () => {
      select(svg).on('.zoom', null);
    };
  }, [bounds.minX, bounds.maxX, height]);

  const zoomBy = useCallback((factor: number) => {
    const svg = svgRef.current;
    if (!svg || !zoomRef.current) return;
    select(svg).transition().duration(180).call(zoomRef.current.scaleBy, factor);
  }, []);

  const resetView = useCallback(() => {
    const svg = svgRef.current;
    if (!svg || !zoomRef.current) return;
    const width = svg.clientWidth || 800;
    const centre = (bounds.minX + bounds.maxX) / 2;
    select(svg)
      .transition()
      .duration(220)
      .call(
        zoomRef.current.transform,
        zoomIdentity.translate(width / 2 - centre * 0.7, height - 90).scale(0.7),
      );
  }, [bounds.minX, bounds.maxX, height]);

  const toggle = useCallback((node: TreeNodeData) => {
    if (!isBranch(node)) return;
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(node.id)) next.delete(node.id);
      else next.add(node.id);
      return next;
    });
  }, []);

  const expandAll = useCallback(() => setCollapsed(new Set()), []);
  const collapseAll = useCallback(() => {
    const next = new Set<string>();
    for (const family of data.children ?? []) {
      next.add(family.id);
      for (const sub of family.children ?? []) next.add(sub.id);
    }
    setCollapsed(next);
  }, [data]);

  function linkPath(link: { source: HierarchyPointNode<TreeNodeData>; target: HierarchyPointNode<TreeNodeData> }) {
    const sx = link.source.x;
    const sy = flipY(link.source.y);
    const tx = link.target.x;
    const ty = flipY(link.target.y);
    const mid = (sy + ty) / 2;
    // Vertical S-curve; upward because the tree is inverted.
    return `M${sx},${sy} C${sx},${mid} ${tx},${mid} ${tx},${ty}`;
  }

  return (
    <div className="relative">
      <div className="absolute right-3 top-3 z-10 flex gap-1">
        {[
          { label: '−', title: 'Zoom out', onClick: () => zoomBy(1 / 1.35) },
          { label: '+', title: 'Zoom in', onClick: () => zoomBy(1.35) },
        ].map((btn) => (
          <button
            key={btn.title}
            type="button"
            title={btn.title}
            aria-label={btn.title}
            onClick={btn.onClick}
            className="size-8 rounded-md border border-line bg-raised text-base text-ink-2 shadow-[var(--shadow)] hover:text-ink"
          >
            {btn.label}
          </button>
        ))}
        <button
          type="button"
          onClick={resetView}
          className="rounded-md border border-line bg-raised px-2.5 text-xs text-ink-2 shadow-[var(--shadow)] hover:text-ink"
        >
          Reset
        </button>
        <button
          type="button"
          onClick={expandAll}
          className="rounded-md border border-line bg-raised px-2.5 text-xs text-ink-2 shadow-[var(--shadow)] hover:text-ink"
        >
          Expand all
        </button>
        <button
          type="button"
          onClick={collapseAll}
          className="rounded-md border border-line bg-raised px-2.5 text-xs text-ink-2 shadow-[var(--shadow)] hover:text-ink"
        >
          Collapse
        </button>
      </div>

      <svg
        ref={svgRef}
        style={{ height }}
        className="w-full cursor-grab touch-none active:cursor-grabbing"
        role="img"
        aria-label="Reverse tree of the workbook: root at the bottom, channels above"
      >
        <g ref={gRef}>
          {links.map((link, i) => (
            <path
              key={i}
              d={linkPath(link)}
              fill="none"
              stroke="var(--line)"
              strokeWidth={1.5}
            />
          ))}

          {nodes.map((node) => {
            const d = node.data;
            const branch = isBranch(d);
            const isCollapsed = collapsed.has(d.id);
            const r = d.kind === 'root' ? 11 : d.kind === 'family' ? 8 : d.kind === 'sub_niche' ? 6 : 5;
            return (
              <g
                key={d.id}
                transform={`translate(${node.x},${flipY(node.y)})`}
                onMouseEnter={() => setHovered(d)}
                onMouseLeave={() => setHovered(null)}
                onClick={() => toggle(d)}
                className={branch ? 'cursor-pointer' : 'cursor-default'}
              >
                <circle
                  r={r}
                  fill={isCollapsed ? 'var(--raised)' : nodeFill(d)}
                  stroke={nodeFill(d)}
                  strokeWidth={2}
                />
                {branch && isCollapsed && (
                  <text
                    textAnchor="middle"
                    dy="0.32em"
                    className="pointer-events-none select-none"
                    style={{ fontSize: 9, fill: 'var(--ink-2)' }}
                  >
                    +
                  </text>
                )}
                <text
                  textAnchor="middle"
                  y={-r - 6}
                  className="pointer-events-none select-none"
                  style={{
                    fontSize: d.kind === 'channel' ? 10 : 11,
                    fill: d.kind === 'channel' ? 'var(--ink-3)' : 'var(--ink)',
                    fontWeight: d.kind === 'root' || d.kind === 'family' ? 500 : 400,
                  }}
                >
                  {d.name.length > 26 ? `${d.name.slice(0, 25)}…` : d.name}
                </text>
                {d.channel_count != null && d.kind !== 'channel' && (
                  <text
                    textAnchor="middle"
                    y={r + 13}
                    className="pointer-events-none select-none tabular-nums"
                    style={{ fontSize: 9, fill: 'var(--ink-3)' }}
                  >
                    {d.channel_count}
                  </text>
                )}
              </g>
            );
          })}
        </g>
      </svg>

      {hovered && (
        <div className="pointer-events-none absolute bottom-3 left-3 max-w-sm rounded-md border border-line bg-raised px-3 py-2 shadow-[var(--shadow)]">
          <p className="text-sm font-medium text-ink">{hovered.name}</p>
          {hovered.kind === 'channel' ? (
            <p className="mt-0.5 text-xs text-ink-2">
              {(hovered.subscriber_count ?? 0).toLocaleString()} subscribers ·{' '}
              {(TRACK_META[(hovered.discovery_method ?? 'unattributed') as DiscoveryMethod] ??
                TRACK_META.unattributed).label}
            </p>
          ) : (
            <p className="mt-0.5 text-xs text-ink-2">
              {hovered.channel_count} channels ·{' '}
              {isBranch(hovered)
                ? collapsed.has(hovered.id)
                  ? 'click to expand'
                  : 'click to collapse'
                : ''}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
