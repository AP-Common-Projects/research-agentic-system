import { useMemo, useState } from 'react';
import type { TreeNodeData } from '../lib/api';
import type { DiscoveryMethod } from '../lib/api';
import { TRACK_META } from './primitives';

/**
 * The workbook explored as an indented, collapsible list.
 *
 * A node-link tree was tried first and was the wrong shape for this data.
 * Finance holds 206 sub-niches across 350 channels -- 1.7 channels each --
 * so the graph was mostly one-child chains, and laid out at ~66,000px wide
 * it could only be explored by panning a canvas whose shape you could never
 * see. Indentation carries the same hierarchy in a column you scroll, which
 * is why file explorers are shaped this way.
 *
 * Size is carried by a proportion bar rather than geometry, so a branch
 * still shows its weight while collapsed, and search reaches every level at
 * once instead of making you hunt for a branch to open.
 */

interface Props {
  root: TreeNodeData;
  query: string;
  /** Family ids to show. Empty means no filter, not "show nothing". */
  families?: Set<string>;
}

const INDENT = 18;

function trackColor(method?: string): string {
  const meta = TRACK_META[(method ?? 'unattributed') as DiscoveryMethod];
  return (meta ?? TRACK_META.unattributed).color;
}

function subsLabel(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
  return String(n);
}

/** Does this node, or anything under it, match the search? */
function matches(node: TreeNodeData, q: string): boolean {
  if (!q) return true;
  if (node.name.toLowerCase().includes(q)) return true;
  return (node.children ?? []).some((child) => matches(child, q));
}

function Row({
  node,
  depth,
  total,
  query,
  openIds,
  toggle,
}: {
  node: TreeNodeData;
  depth: number;
  total: number;
  query: string;
  openIds: Set<string>;
  toggle: (id: string) => void;
}) {
  const children = node.children ?? [];
  const isBranch = children.length > 0;
  // While searching, branches auto-open so a match is never hidden behind a
  // closed parent -- otherwise the result you searched for is invisible.
  const open = query ? true : openIds.has(node.id);
  const visibleChildren = query
    ? children.filter((c) => matches(c, query))
    : children;

  const count = node.channel_count ?? (node.kind === 'channel' ? 1 : 0);
  const share = total > 0 ? (count / total) * 100 : 0;
  const isChannel = node.kind === 'channel';
  const hit = query && node.name.toLowerCase().includes(query);

  return (
    <li>
      <div
        role={isBranch ? 'button' : undefined}
        tabIndex={isBranch ? 0 : undefined}
        aria-expanded={isBranch ? open : undefined}
        onClick={() => isBranch && toggle(node.id)}
        onKeyDown={(e) => {
          if (isBranch && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault();
            toggle(node.id);
          }
        }}
        style={{ paddingLeft: depth * INDENT + 8 }}
        className={`group flex items-center gap-2 rounded py-1.5 pr-3 text-sm ${
          isBranch ? 'cursor-pointer hover:bg-sunken' : ''
        } ${hit ? 'bg-sunken' : ''}`}
      >
        {isBranch ? (
          <span
            aria-hidden
            className={`select-none text-xs text-ink-3 transition-transform ${
              open ? 'rotate-90' : ''
            }`}
          >
            ▶
          </span>
        ) : (
          <span
            aria-hidden
            className="size-2 shrink-0 rounded-full"
            style={{ background: trackColor(node.discovery_method) }}
          />
        )}

        <span
          className={`min-w-0 flex-1 truncate ${
            isChannel ? 'text-ink-2' : 'font-medium text-ink'
          }`}
          title={node.name}
        >
          {node.name}
        </span>

        {isChannel ? (
          <span className="shrink-0 tabular-nums text-xs text-ink-3">
            {subsLabel(node.subscriber_count ?? 0)}
          </span>
        ) : (
          <>
            <span
              className="hidden h-1.5 w-24 shrink-0 overflow-hidden rounded-full bg-sunken sm:block"
              title={`${share.toFixed(1)}% of the workbook`}
            >
              <span
                className="block h-full rounded-full bg-[var(--focus)]"
                style={{ width: `${Math.max(share, 1.5)}%` }}
              />
            </span>
            <span className="w-10 shrink-0 text-right tabular-nums text-xs text-ink-3">
              {count}
            </span>
          </>
        )}
      </div>

      {open && visibleChildren.length > 0 && (
        <ul>
          {visibleChildren.map((child) => (
            <Row
              key={child.id}
              node={child}
              depth={depth + 1}
              total={total}
              query={query}
              openIds={openIds}
              toggle={toggle}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

export function TreeExplorer({ root, query, families: familyFilter }: Props) {
  // Families open, sub-niches closed: the family level is where the shape of
  // a workbook actually reads, and opening all 206 sub-niches at once is a
  // wall of one-channel rows.
  const [openIds, setOpenIds] = useState<Set<string>>(
    () => new Set((root.children ?? []).map((f) => f.id)),
  );

  const q = query.trim().toLowerCase();
  const total = root.channel_count ?? 0;

  const families = useMemo(() => {
    const all = root.children ?? [];
    // An empty filter set means "no family filter applied". Treating it as
    // "show none" would blank the page the moment someone cleared the last
    // chip, which reads as a bug rather than a reset.
    const picked =
      familyFilter && familyFilter.size > 0
        ? all.filter((f) => familyFilter.has(f.id))
        : all;
    return picked.filter((f) => matches(f, q));
  }, [root, q, familyFilter]);

  function toggle(id: string) {
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  if (families.length === 0) {
    return (
      <p className="px-4 py-8 text-center text-sm text-ink-2">
        {query
          ? `Nothing matches “${query}”${
              familyFilter && familyFilter.size > 0
                ? ' in the selected families'
                : ''
            }.`
          : 'No families selected.'}
      </p>
    );
  }

  return (
    <ul className="max-h-[560px] overflow-y-auto py-1">
      {families.map((family) => (
        <Row
          key={family.id}
          node={family}
          depth={0}
          total={total}
          query={q}
          openIds={openIds}
          toggle={toggle}
        />
      ))}
    </ul>
  );
}
