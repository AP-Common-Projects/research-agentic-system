import { useQuery } from '@tanstack/react-query';
import { api, type Workbook } from '../lib/api';
import { Panel, Eyebrow, EmptyState, ErrorState, Skeleton } from '../components/primitives';

function fileSize(bytes: number): string {
  return bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.round(bytes / 1e3)} KB`;
}

function when(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

function WorkbookCard({ workbook }: { workbook: Workbook }) {
  if (!workbook.available) {
    return (
      <Panel>
        <div className="p-5">
          <h2 className="font-medium text-ink">{workbook.title}</h2>
          <p className="mt-2 text-sm text-ink-2">
            {workbook.error ?? 'This workbook is not on disk.'}
          </p>
        </div>
      </Panel>
    );
  }

  // Sheets in file order, which is the order a reader opens them in.
  const sheets = workbook.sheets ?? [];

  return (
    <Panel>
      <div className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-medium text-ink">{workbook.title}</h2>
            <p className="mt-0.5 text-sm text-ink-2">{workbook.description}</p>
            <p className="mt-1 font-mono text-xs text-ink-3">{workbook.filename}</p>
          </div>
          <a
            href={`${import.meta.env.DEV ? 'http://localhost:8000' : ''}${workbook.download_url}`}
            className="shrink-0 rounded-md bg-[var(--focus)] px-4 py-2 text-sm font-medium text-white"
          >
            Download
          </a>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[
            ['Channels', workbook.channel_count?.toLocaleString() ?? '—'],
            ['Videos', workbook.video_count?.toLocaleString() ?? '—'],
            ['Size', workbook.size_bytes ? fileSize(workbook.size_bytes) : '—'],
            ['Updated', workbook.modified_at ? when(workbook.modified_at) : '—'],
          ].map(([label, value]) => (
            <div key={label} className="rounded-md border border-line bg-sunken px-3 py-2">
              <dt className="text-xs text-ink-3">{label}</dt>
              <dd className="mt-0.5 text-sm font-medium tabular-nums text-ink">{value}</dd>
            </div>
          ))}
        </div>

        {sheets.length > 0 && (
          <div className="mt-4">
            <p className="mb-2 text-xs text-ink-3">
              {sheets.length} sheets — row counts read from the file itself
            </p>
            <ul className="flex flex-wrap gap-1.5">
              {sheets.map((sheet) => (
                <li
                  key={sheet.name}
                  className="flex items-baseline gap-1.5 rounded border border-line bg-raised px-2.5 py-1 text-xs"
                >
                  <span className="text-ink-2">{sheet.name}</span>
                  <span className="tabular-nums text-ink-3">
                    {sheet.rows.toLocaleString()}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Panel>
  );
}

export function WorkbooksPage() {
  const workbooks = useQuery({ queryKey: ['workbooks'], queryFn: api.workbooks });

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6">
      <header>
        <Eyebrow>Deliverables</Eyebrow>
        <h1 className="mt-1 text-2xl font-medium text-ink">Exported workbooks</h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-2">
          The finished Excel research reports, ready to send. Figures below are
          read from each file directly, so this page cannot drift from what the
          client would actually open.
        </p>
      </header>

      {workbooks.isLoading && <Skeleton rows={4} />}
      {workbooks.isError && (
        <ErrorState
          title="Could not load workbooks"
          detail={(workbooks.error as Error)?.message}
        />
      )}
      {workbooks.data?.length === 0 && (
        <EmptyState title="No workbooks yet">
          A run has to finish and export before anything appears here.
        </EmptyState>
      )}
      {workbooks.data?.map((workbook) => (
        <WorkbookCard key={workbook.id} workbook={workbook} />
      ))}
    </div>
  );
}
