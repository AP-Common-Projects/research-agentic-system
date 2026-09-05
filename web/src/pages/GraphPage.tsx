import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type DiscoveryMethod } from '../lib/api';
import { WorkbookPicker } from '../components/WorkbookPicker';
import { ReverseTree } from '../components/ReverseTree';
import {
  Panel,
  PanelHeader,
  Eyebrow,
  ErrorState,
  Skeleton,
  TRACK_META,
} from '../components/primitives';

export function GraphPage() {
  const [workbookId, setWorkbookId] = useState<string | null>(null);
  const workbooks = useQuery({ queryKey: ['workbooks'], queryFn: api.workbooks });
  const active = workbookId ?? workbooks.data?.find((w) => w.available)?.id ?? null;

  const tree = useQuery({
    queryKey: ['workbook-tree', active],
    queryFn: () => api.workbookTree(active as string),
    enabled: !!active,
  });

  // Track counts come from the leaves, so the legend matches what is drawn.
  const trackCounts: Record<string, number> = {};
  if (tree.data) {
    for (const family of tree.data.children ?? []) {
      for (const sub of family.children ?? []) {
        for (const channel of sub.children ?? []) {
          const key = channel.discovery_method ?? 'unattributed';
          trackCounts[key] = (trackCounts[key] ?? 0) + 1;
        }
      }
    }
  }

  return (
    <div className="mx-auto max-w-6xl space-y-5 p-6">
      <header>
        <Eyebrow>Discovery graph</Eyebrow>
        <h1 className="mt-1 text-2xl font-medium text-ink">
          How this workbook is composed
        </h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-2">
          The deliverable as a reverse tree — the vertical at the root below,
          growing up through niche families and sub-niches to the channels
          themselves. Scroll to zoom, drag to pan, click any branch to open or
          close it.
        </p>
      </header>

      <WorkbookPicker value={active} onChange={setWorkbookId} />

      {tree.isLoading && <Skeleton rows={5} />}
      {tree.isError && (
        <ErrorState
          title="Could not load the tree"
          detail={(tree.error as Error)?.message}
        />
      )}

      {tree.data && (
        <>
          <div className="flex flex-wrap items-center gap-4">
            {Object.entries(trackCounts).map(([method, count]) => {
              const meta =
                TRACK_META[method as DiscoveryMethod] ?? TRACK_META.unattributed;
              return (
                <div key={method} className="flex items-center gap-2 text-sm">
                  <span
                    aria-hidden
                    className="size-2.5 rounded-full"
                    style={{ background: meta.color }}
                  />
                  <span className="text-ink-2">{meta.label}</span>
                  <span className="tabular-nums text-ink-3">{count}</span>
                </div>
              );
            })}
          </div>

          <Panel>
            <PanelHeader
              title={`${tree.data.channel_count} channels`}
              hint={`${tree.data.children?.length ?? 0} niche families — sub-niches start closed`}
            />
            <ReverseTree data={tree.data} height={580} />
          </Panel>
        </>
      )}
    </div>
  );
}
