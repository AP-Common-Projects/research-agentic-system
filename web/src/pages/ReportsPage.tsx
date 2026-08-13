import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import {
  EmptyState,
  ErrorState,
  Eyebrow,
  GradeTally,
  Panel,
  PanelHeader,
  Skeleton,
} from '../components/primitives';
import { compactNumber, relativeTime } from '../lib/format';

export function ReportsPage() {
  const reports = useQuery({
    queryKey: ['reports'],
    queryFn: api.reports,
    refetchInterval: 15_000,
  });

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <header className="mb-6">
        <Eyebrow>Console</Eyebrow>
        <h1 className="mt-1 font-display text-2xl font-semibold tracking-tight text-balance text-ink">
          Reports
        </h1>
        <p className="mt-1.5 max-w-2xl text-sm text-ink-2">
          Every run that reached synthesize, newest first. A run still in progress, or one that
          never produced a report, doesn't appear here — see Runs for those.
        </p>
      </header>

      <Panel>
        <PanelHeader
          title="Library"
          right={
            reports.isFetching ? (
              <span className="font-mono text-[10px] text-ink-3">syncing</span>
            ) : null
          }
        />
        {reports.isLoading ? (
          <Skeleton rows={4} />
        ) : reports.isError ? (
          <div className="p-4">
            <ErrorState
              title="Could not load reports"
              detail={reports.error instanceof Error ? reports.error.message : undefined}
            />
          </div>
        ) : reports.data && reports.data.length > 0 ? (
          <ul className="divide-y divide-line">
            {reports.data.map((report) => {
              const totalChannels = report.discovery_stats?.total_channels;
              return (
                <li key={report.run_id}>
                  <Link
                    to={`/runs/${report.run_id}?view=report`}
                    className="block px-4 py-3.5 transition-colors hover:bg-sunken"
                  >
                    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                      <span className="font-display text-sm font-medium text-ink">
                        {report.niche || report.run_id}
                      </span>
                      <span className="font-mono text-[11px] text-ink-3">
                        {relativeTime(report.generated_at)}
                      </span>
                    </div>

                    {report.summary ? (
                      <p className="mt-1.5 line-clamp-2 text-sm text-ink-2">{report.summary}</p>
                    ) : null}

                    <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5">
                      <GradeTally counts={report.grade_counts} />
                      {typeof totalChannels === 'number' ? (
                        <span className="font-mono text-xs text-ink-3">
                          {compactNumber(totalChannels)} channels
                        </span>
                      ) : null}
                      {report.cannot_determine_count > 0 ? (
                        <span className="font-mono text-xs text-ink-3">
                          {report.cannot_determine_count} unresolved
                        </span>
                      ) : null}
                    </div>
                  </Link>
                </li>
              );
            })}
          </ul>
        ) : (
          <EmptyState title="No reports yet">
            Reports show up here once a run reaches synthesize — check Runs to see what's in
            progress.
          </EmptyState>
        )}
      </Panel>
    </div>
  );
}
