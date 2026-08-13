import { useDeferredValue, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import {
  EmptyState,
  ErrorState,
  Eyebrow,
  Panel,
  PanelHeader,
  Skeleton,
  StatTile,
  TrackBadge,
  TRACK_META,
} from '../components/primitives';
import { BarList } from '../components/charts';
import { compactNumber, decimal, relativeTime } from '../lib/format';

function ChannelsTab() {
  const [query, setQuery] = useState('');
  // Keeps typing responsive while a large result set re-renders.
  const deferred = useDeferredValue(query);

  const channels = useQuery({
    queryKey: ['channels', deferred],
    queryFn: () => api.channels(deferred, 100),
  });

  return (
    <Panel>
      <PanelHeader
        title="Channels"
        hint="Ordered by subscriber count."
        right={
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search title or ID…"
            spellCheck={false}
            aria-label="Search channels"
            name="channel-search"
            autoComplete="off"
            className="w-56 rounded-md border border-line bg-page px-2.5 py-1.5 text-xs text-ink placeholder:text-ink-3"
          />
        }
      />
      {channels.isLoading ? (
        <Skeleton rows={5} />
      ) : channels.isError ? (
        <div className="p-4">
          <ErrorState
            title="Could not load channels"
            detail={channels.error instanceof Error ? channels.error.message : undefined}
          />
        </div>
      ) : channels.data && channels.data.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left">
                <th scope="col" className="px-4 py-2 font-mono text-[10px] tracking-wide text-ink-3 uppercase">
                  Channel
                </th>
                <th scope="col" className="px-4 py-2 font-mono text-[10px] tracking-wide text-ink-3 uppercase">
                  Found by
                </th>
                <th scope="col" className="px-4 py-2 text-right font-mono text-[10px] tracking-wide text-ink-3 uppercase">
                  Subscribers
                </th>
                <th scope="col" className="px-4 py-2 text-right font-mono text-[10px] tracking-wide text-ink-3 uppercase">
                  First seen
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {channels.data.map((c) => (
                <tr key={c.channel_id} className="hover:bg-sunken">
                  <td className="px-4 py-2.5">
                    <div className="max-w-md truncate text-ink">{c.title ?? '(untitled)'}</div>
                    <div className="font-mono text-[11px] text-ink-3">{c.channel_id}</div>
                  </td>
                  <td className="px-4 py-2.5">
                    <TrackBadge method={c.discovery_method} />
                  </td>
                  <td className="tnum px-4 py-2.5 text-right font-mono text-ink-2">
                    {compactNumber(c.subscriber_count)}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-[11px] text-ink-3">
                    {relativeTime(c.first_seen_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState title="No channels match">
          {query ? 'Try a broader search.' : 'The store fills in as runs hydrate metadata.'}
        </EmptyState>
      )}
    </Panel>
  );
}

function OutliersTab() {
  const outliers = useQuery({ queryKey: ['outliers'], queryFn: () => api.outliers(100) });

  return (
    <Panel>
      <PanelHeader
        title="Outlier videos"
        hint="Views relative to the channel’s own neighbouring uploads — 2–3× repeated is a pattern, one 10× spike is a fluke."
      />
      {outliers.isLoading ? (
        <Skeleton rows={5} />
      ) : outliers.isError ? (
        <div className="p-4">
          <ErrorState
            title="Could not load videos"
            detail={outliers.error instanceof Error ? outliers.error.message : undefined}
          />
        </div>
      ) : outliers.data && outliers.data.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left">
                <th scope="col" className="px-4 py-2 font-mono text-[10px] tracking-wide text-ink-3 uppercase">
                  Video
                </th>
                <th scope="col" className="px-4 py-2 font-mono text-[10px] tracking-wide text-ink-3 uppercase">
                  Found by
                </th>
                <th scope="col" className="px-4 py-2 text-right font-mono text-[10px] tracking-wide text-ink-3 uppercase">
                  Outlier
                </th>
                <th scope="col" className="px-4 py-2 text-right font-mono text-[10px] tracking-wide text-ink-3 uppercase">
                  Views
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {outliers.data.map((v) => (
                <tr key={v.video_id} className="hover:bg-sunken">
                  <td className="px-4 py-2.5">
                    <div className="max-w-md truncate text-ink">{v.title ?? '(untitled)'}</div>
                    <div className="truncate font-mono text-[11px] text-ink-3">
                      {v.channel_title ?? v.channel_id}
                    </div>
                  </td>
                  <td className="px-4 py-2.5">
                    <TrackBadge method={v.discovery_method} />
                  </td>
                  <td className="tnum px-4 py-2.5 text-right font-mono font-medium text-ink">
                    {v.outlier_score ? `${decimal(v.outlier_score)}×` : '—'}
                  </td>
                  <td className="tnum px-4 py-2.5 text-right font-mono text-ink-2">
                    {compactNumber(v.view_count)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState title="No scored videos yet">
          Outlier scores are computed during hydrate_metadata.
        </EmptyState>
      )}
    </Panel>
  );
}

export function ChannelsPage() {
  const [tab, setTab] = useState<'channels' | 'outliers'>('channels');
  const counts = useQuery({ queryKey: ['counts'], queryFn: api.counts });

  const breakdown =
    counts.data?.by_discovery_method.map((row) => ({
      key: row.discovery_method,
      label: (TRACK_META[row.discovery_method] ?? TRACK_META.unattributed).label,
      value: row.channel_count,
      display: compactNumber(row.channel_count),
      color: (TRACK_META[row.discovery_method] ?? TRACK_META.unattributed).color,
    })) ?? [];

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <header className="mb-6">
        <Eyebrow>Console</Eyebrow>
        <h1 className="mt-1 font-display text-2xl font-semibold tracking-tight text-balance text-ink">
          Channels
        </h1>
      </header>

      <div className="mb-5 grid gap-5 lg:grid-cols-[1fr_320px]">
        <div className="grid grid-cols-3 gap-3">
          <StatTile label="Channels" value={compactNumber(counts.data?.channels)} />
          <StatTile label="Videos" value={compactNumber(counts.data?.videos)} />
          <StatTile label="Edges" value={compactNumber(counts.data?.discovery_edges)} />
        </div>
        <Panel>
          <PanelHeader title="How they were found" />
          <BarList rows={breakdown} emptyLabel="No channels in the store yet." />
        </Panel>
      </div>

      <div className="mb-5 flex gap-1 border-b border-line" role="tablist" aria-label="Store views">
        {(
          [
            ['channels', 'Channels'],
            ['outliers', 'Outlier videos'],
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

      {tab === 'channels' ? <ChannelsTab /> : <OutliersTab />}
    </div>
  );
}
