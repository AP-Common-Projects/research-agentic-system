import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../lib/api';
import { WorkbookPicker } from '../components/WorkbookPicker';
import { Panel, PanelHeader, Eyebrow, ErrorState, Skeleton } from '../components/primitives';

function usd(value: number): string {
  return `$${value.toFixed(2)}`;
}

/** One provider's contribution to a workbook's cost. */
function ProviderTotal({
  label,
  amount,
  total,
  detail,
  tint,
}: {
  label: string;
  amount: number;
  total: number;
  detail: string;
  tint: string;
}) {
  const share = total > 0 ? (amount / total) * 100 : 0;
  return (
    <div className="rounded-lg border border-line bg-raised p-4">
      <div className="flex items-baseline justify-between">
        <span className="text-sm text-ink-2">{label}</span>
        <span className="tabular-nums text-xs text-ink-3">{share.toFixed(0)}%</span>
      </div>
      <p className="mt-1 text-2xl font-medium tabular-nums text-ink">{usd(amount)}</p>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-sunken">
        <div
          className="h-full rounded-full"
          style={{ width: `${share}%`, background: tint }}
        />
      </div>
      <p className="mt-2 text-xs leading-snug text-ink-3">{detail}</p>
    </div>
  );
}

export function SpendPage() {
  const [workbookId, setWorkbookId] = useState<string | null>(null);
  const workbooks = useQuery({ queryKey: ['workbooks'], queryFn: api.workbooks });

  // Default to the first available workbook so the page is never empty.
  const active =
    workbookId ?? workbooks.data?.find((w) => w.available)?.id ?? null;

  const spend = useQuery({
    queryKey: ['workbook-spend', active],
    queryFn: () => api.workbookSpend(active as string),
    enabled: !!active,
  });

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6">
      <header>
        <Eyebrow>Spend</Eyebrow>
        <h1 className="mt-1 text-2xl font-medium text-ink">What a workbook cost</h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-2">
          Discovery and model spend for one finished deliverable, attributed
          run by run from the ledgers each run wrote.
        </p>
      </header>

      <WorkbookPicker value={active} onChange={setWorkbookId} />

      {spend.isLoading && <Skeleton rows={4} />}
      {spend.isError && (
        <ErrorState
          title="Could not load spend"
          detail={(spend.error as Error)?.message}
        />
      )}

      {spend.data && (
        <>
          <Panel>
            <div className="p-5">
              <p className="text-xs text-ink-3">Total attributed</p>
              <p className="mt-1 text-4xl font-medium tabular-nums text-ink">
                {usd(spend.data.total_usd)}
              </p>
              <p className="mt-1 text-sm text-ink-2">
                across {spend.data.attributed_run_count} run
                {spend.data.attributed_run_count === 1 ? '' : 's'}
                {spend.data.run_count !== spend.data.attributed_run_count &&
                  ` (${spend.data.run_count} touched this workbook)`}
              </p>

              <div className="mt-4 grid gap-3 sm:grid-cols-2">
                <ProviderTotal
                  label="Bright Data — discovery"
                  amount={spend.data.brightdata_usd}
                  total={spend.data.total_usd}
                  tint="var(--track-keyword)"
                  detail={`${spend.data.brightdata_records.toLocaleString()} records billed at $${spend.data.cost_per_record_usd}`}
                />
                <ProviderTotal
                  label="OpenRouter — model"
                  amount={spend.data.openrouter_usd}
                  total={spend.data.total_usd}
                  tint="var(--track-graph)"
                  detail="Classification, enrichment and case metadata calls"
                />
              </div>

              <p className="mt-4 rounded border border-line bg-sunken px-3 py-2 text-xs leading-snug text-ink-2">
                {spend.data.note}
              </p>
            </div>
          </Panel>

          <Panel>
            <PanelHeader title="By run" hint="Largest first" />
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-ink-3">
                  <th className="px-4 py-2 font-normal">Run</th>
                  <th className="px-4 py-2 text-right font-normal">Records</th>
                  <th className="px-4 py-2 text-right font-normal">Discovery</th>
                  <th className="px-4 py-2 text-right font-normal">Model</th>
                </tr>
              </thead>
              <tbody>
                {spend.data.by_run.map((run) => (
                  <tr key={run.run_id} className="border-b border-line last:border-0">
                    <td className="px-4 py-2 font-mono text-xs text-ink-2">
                      {run.run_id}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums text-ink-2">
                      {run.records.toLocaleString()}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums text-ink">
                      {usd(run.discovery_usd)}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums text-ink">
                      {usd(run.model_usd)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
        </>
      )}
    </div>
  );
}
