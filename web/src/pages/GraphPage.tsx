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
  picked,
}: {
  families: TreeNodeData[];
  total: number;
  onPick: (id: string) => void;
  picked: Set<string>;
}) {
  const filtering = picked.size > 0;
  return (
    <div>
      <div className="flex h-8 w-full overflow-hidden rounded-md border border-line">
        {families.map((family, i) => {
          const share = total > 0 ? ((family.channel_count ?? 0) / total) * 100 : 0;
          return (
            <button
              key={family.id}
              type="button"
              onClick={() => onPick(family.id)}
              title={`${family.name} — ${family.channel_count} channels (${share.toFixed(1)}%)`}
              className="h-full transition-opacity hover:opacity-90"
              style={{
                width: `${share}%`,
                background: FAMILY_TINTS[i % FAMILY_TINTS.length],
                // Dim the unpicked rather than hiding them: the bar is a
                // part-to-whole read, so the whole has to stay visible.
                opacity: filtering && !picked.has(family.id) ? 0.25 : 1,
              }}
              aria-label={`${family.name}, ${family.channel_count} channels`}
            />
          );
        })}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {families.map((family, i) => {
          const on = picked.has(family.id);
          return (
            <button
              key={family.id}
              type="button"
              onClick={() => onPick(family.id)}
              aria-pressed={on}
              className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors ${
                on
                  ? 'border-[var(--focus)] bg-raised text-ink'
                  : 'border-line text-ink-2 hover:text-ink'
              }`}
            >
              <span
                aria-hidden
                className="size-2 rounded-sm"
                style={{
                  background: FAMILY_TINTS[i % FAMILY_TINTS.length],
                  opacity: filtering && !on ? 0.35 : 1,
                }}
              />
              {family.name}
              <span className="tabular-nums text-ink-3">{family.channel_count}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function GraphPage() {
  const [workbookId, setWorkbookId] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [pickedFamilies, setPickedFamilies] = useState<Set<string>>(new Set());

  const workbooks = useQuery({ queryKey: ['workbooks'], queryFn: api.workbooks });
  const active = workbookId ?? workbooks.data?.find((w) => w.available)?.id ?? null;

  function selectWorkbook(id: string) {
    setWorkbookId(id);
    // Family ids are per-vertical, so a Finance selection means nothing in
    // Crime and would silently filter everything out.
    setPickedFamilies(new Set());
    setQuery('');
  }

  function toggleFamily(id: string) {
    setPickedFamilies((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

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
  const shownChannels = families
    .filter((f) => pickedFamilies.size === 0 || pickedFamilies.has(f.id))
    .reduce((sum, f) => sum + (f.channel_count ?? 0), 0);

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6">
      <header>
        <Eyebrow>Discovery graph</Eyebrow>
        <h1 className="mt-1 text-2xl font-medium text-ink">
          How this workbook is composed
        </h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-2">
          Every channel in the workbook, grouped by niche family and
          sub-niche. Open a branch to look inside, or search to jump straight
          to anything.
        </p>
      </header>

      <WorkbookPicker value={active} onChange={selectWorkbook} />

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
              hint="Click a family to focus on it — click again to bring the rest back"
            />
            <div className="p-4">
              <CompositionBar
                families={families}
                total={tree.data.channel_count ?? 0}
                onPick={toggleFamily}
                picked={pickedFamilies}
              />
            </div>
          </Panel>

          <Panel>
            <PanelHeader
              title="Explore"
              hint={
                pickedFamilies.size > 0
                  ? `${shownChannels} of ${tree.data.channel_count} channels in ${pickedFamilies.size} famil${
                      pickedFamilies.size === 1 ? 'y' : 'ies'
                    }`
                  : 'Families are open; open a sub-niche to see its channels'
              }
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
              {(query || pickedFamilies.size > 0) && (
                <div className="mt-2 flex gap-3">
                  {query && (
                    <button
                      type="button"
                      onClick={() => setQuery('')}
                      className="text-xs text-[var(--focus)] hover:underline"
                    >
                      Clear search
                    </button>
                  )}
                  {pickedFamilies.size > 0 && (
                    <button
                      type="button"
                      onClick={() => setPickedFamilies(new Set())}
                      className="text-xs text-[var(--focus)] hover:underline"
                    >
                      Show all families
                    </button>
                  )}
                </div>
              )}
            </div>
            <TreeExplorer root={tree.data} query={query} families={pickedFamilies} />
          </Panel>
        </>
      )}
    </div>
  );
}
