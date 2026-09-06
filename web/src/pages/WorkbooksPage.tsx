import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type Workbook } from '../lib/api';
import {
  Panel,
  Eyebrow,
  EmptyState,
  ErrorState,
  Skeleton,
  ConfirmDialog,
  DangerButton,
} from '../components/primitives';

function fileSize(bytes: number): string {
  return bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.round(bytes / 1e3)} KB`;
}

function when(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

//: The curated deliverables, which this page never offers to delete.
const PINNED_WORKBOOKS = new Set(['finance', 'crime']);

function WorkbookCard({
  workbook,
  onAskDelete,
}: {
  workbook: Workbook;
  onAskDelete: (workbook: Workbook) => void;
}) {
  const deletable = !PINNED_WORKBOOKS.has(workbook.id);
  if (!workbook.available) {
    return (
      <Panel>
        <div className="p-5">
          <h2 className="font-medium text-ink">{workbook.title}</h2>
          <p className="mt-2 text-sm text-ink-2">
            {workbook.error ?? 'We couldn\u2019t find this file.'}
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
          <div className="flex shrink-0 items-center gap-2">
            {/* The two delivered workbooks have no delete: they are the
                shipped client work, they sit outside the per-run export
                layout, and a re-export would not reproduce them because the
                runs behind them have had manual backfills since. The API
                refuses them too, so this is not the only guard. */}
            {deletable && (
              <DangerButton
                onClick={() => onAskDelete(workbook)}
                title="Removes the exported files. The run's data stays in the database, so it can be exported again."
              >
                Delete
              </DangerButton>
            )}
            <a
              href={`${import.meta.env.DEV ? 'http://localhost:8000' : ''}${workbook.download_url}`}
              className="rounded-md bg-[var(--focus)] px-4 py-2 text-sm font-medium text-white"
            >
              Download
            </a>
          </div>
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
              {sheets.length} sheets, with the row count in each
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
  const queryClient = useQueryClient();
  const [pendingDelete, setPendingDelete] = useState<Workbook | null>(null);
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteWorkbook(id),
    onSuccess: () => {
      setPendingDelete(null);
      queryClient.invalidateQueries({ queryKey: ['workbooks'] });
    },
  });

  const workbooks = useQuery({ queryKey: ['workbooks'], queryFn: api.workbooks });

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6">
      <header>
        <Eyebrow>Deliverables</Eyebrow>
        <h1 className="mt-1 text-2xl font-medium text-ink">Exported workbooks</h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-2">
          Your finished research workbooks, ready to share. The figures here
          are read straight from each file, so what you see is exactly
          what&rsquo;s inside it.
        </p>
      </header>

      {remove.isError && (
        <ErrorState
          title="Could not delete that workbook"
          detail={(remove.error as Error).message}
        />
      )}

      {workbooks.isLoading && <Skeleton rows={4} />}
      {workbooks.isError && (
        <ErrorState
          title="Could not load workbooks"
          detail={(workbooks.error as Error)?.message}
        />
      )}
      {workbooks.data?.length === 0 && (
        <EmptyState title="No workbooks yet">
          Once a run finishes, its workbook will show up here.
        </EmptyState>
      )}
      {workbooks.data?.map((workbook) => (
        <WorkbookCard
          key={workbook.id}
          workbook={workbook}
          onAskDelete={setPendingDelete}
        />
      ))}

      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete this workbook?"
        body={
          <>
            <p>
              <span className="font-medium text-ink">{pendingDelete?.title}</span>{' '}
              and the files exported alongside it — the CSVs, the discovery
              graph and the manifest — will be removed from disk.
            </p>
            <p className="mt-2">
              The run&rsquo;s data stays in the database, so this workbook can
              be exported again from the same run.
            </p>
          </>
        }
        confirmLabel="Delete workbook"
        pending={remove.isPending}
        onConfirm={() => pendingDelete && remove.mutate(pendingDelete.id)}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
