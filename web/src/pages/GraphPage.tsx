import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type DiscoveryMethod, type TreeNodeData } from '../lib/api';
import { WorkbookPicker } from '../components/WorkbookPicker';
import { TreeExplorer } from '../components/TreeExplorer';
import {
  Panel,
  PanelHeader,
  Eyebrow,
  ErrorState,
  Skeleton,
  TRACK_META,
} from '../components/primitives';

// Ordinal ramp over one hue: families are ranked by size, not categorical,
// so a rainbow would imply difference in kind rather than degree.
const FAMILY_TINTS = [
  '#0e7490', '#0f8ba8', '#16a0b8', '#2fb3c6', '#55c4d3',
  '#7bd3df', '#9fe0e9', '#bde9ef', '#d4f0f4', '#e4f5f8',
];

/** Part-to-whole strip: the shape of the workbook before you open anything. */
function CompositionBar({
  families,
  total,
  onPick,
}: {
  families: TreeNodeData[];
  total: number;
  onPick: (name: string) => void;
}) {
  return (
    <div>
      <div className="flex h-8 w-full overflow-hidden rounded-md border border-line">
        {families.map((family, i) => {
          const share = total > 0 ? ((family.channel_count ?? 0) / total) * 100 : 0;
          return (
            <button
              key={family.id}
              type="button"
              onClick={() => onPick(family.name)}
              title={`${family.name} — ${family.channel_count} channels (${share.toFixed(1)}%)`}
              className="h-full transition-opacity hover:opacity-80"
              style={{
                width: `${share}%`,
                background: FAMILY_TINTS[i % FAMILY_TINTS.length],
              }}
              aria-label={`${family.name}, ${family.channel_count} channels`}
            />
          );
        })}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {families.slice(0, 8).map((family, i) => (
          <button
            key={family.id}
            type="button"
            onClick={() => onPick(family.name)}
            className="flex items-center gap-1.5 text-xs text-ink-2 hover:text-ink"
          >
            <span
              aria-hidden
              className="size-2 rounded-sm"
              style={{ background: FAMILY_TINTS[i % FAMILY_TINTS.length] }}
            />
            {family.name}
            <span className="tabular-nums text-ink-3">{family.channel_count}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

export function GraphPage() {
  const [workbookId, setWorkbookId] = useState<string | null>(null);
  const [query, setQuery] = useState('');

  const workbooks = useQuery({ queryKey: ['workbooks'], queryFn: api.workbooks });
  const active = workbookId ?? workbooks.data?.find((w) => w.available)?.id ?? null;

  const tree = useQuery({
    queryKey: ['workbook-tree', active],
    queryFn: () => api.workbookTree(active as string),
    enabled: !!active,
  });

  const trackCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const family of tree.data?.children ?? []) {
      for (const sub of family.children ?? []) {
        for (const channel of sub.children ?? []) {
          const key = channel.discovery_method ?? 'unattributed';
          counts[key] = (counts[key] ?? 0) + 1;
        }
      }
    }
    return counts;
  }, [tree.data]);

  const families = tree.data?.children ?? [];

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6">
      <header>
        <Eyebrow>Discovery graph</Eyebrow>
        <h1 className="mt-1 text-2xl font-medium text-ink">
          How this workbook is composed
        </h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-2">
          Every channel that shipped, grouped by niche family and sub-niche.
          Open a branch to drill in, or search to jump straight to anything at
          any level.
        </p>
      </header>

      <WorkbookPicker value={active} onChange={setWorkbookId} />

      {tree.isLoading && <Skeleton rows={5} />}
      {tree.isError && (
        <ErrorState
          title="Could not load the structure"
          detail={(tree.error as Error)?.message}
        />
      )}

      {tree.data && (
        <>
          <Panel>
            <PanelHeader
              title="Composition"
              hint={`${tree.data.channel_count} channels across ${families.length} families`}
            />
            <div className="p-4">
              <CompositionBar
                families={families}
                total={tree.data.channel_count ?? 0}
                onPick={setQuery}
              />
            </div>
          </Panel>

          <Panel>
            <PanelHeader
              title="Explore"
              hint="Families open, sub-niches closed"
              right={
                <div className="flex items-center gap-2">
                  {Object.entries(trackCounts).map(([method, count]) => {
                    const meta =
                      TRACK_META[method as DiscoveryMethod] ??
                      TRACK_META.unattributed;
                    return (
                      <span
                        key={method}
                        className="flex items-center gap-1.5 text-xs text-ink-2"
                        title={meta.note}
                      >
                        <span
                          aria-hidden
                          className="size-2 rounded-full"
                          style={{ background: meta.color }}
                        />
                        {meta.label}
                        <span className="tabular-nums text-ink-3">{count}</span>
                      </span>
                    );
                  })}
                </div>
              }
            />
            <div className="border-b border-line p-3">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search channels, sub-niches or families…"
                aria-label="Search the workbook"
                className="w-full rounded-md border border-line bg-raised px-3 py-2 text-sm text-ink outline-none placeholder:text-ink-3 focus:border-[var(--focus)]"
              />
              {query && (
                <button
                  type="button"
                  onClick={() => setQuery('')}
                  className="mt-2 text-xs text-[var(--focus)] hover:underline"
                >
                  Clear search
                </button>
              )}
            </div>
            <TreeExplorer root={tree.data} query={query} />
          </Panel>
        </>
      )}
    </div>
  );
}
