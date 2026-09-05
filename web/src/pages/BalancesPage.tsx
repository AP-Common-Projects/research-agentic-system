import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type DepthTier, type ProviderBalance } from '../lib/api';
import { Panel, PanelHeader, Eyebrow, ErrorState, Skeleton } from '../components/primitives';

const PROVIDER_META: Record<string, { label: string; funds: string; topUp: string }> = {
  openrouter: {
    label: 'OpenRouter',
    funds: 'Analysing channels — classification, enrichment, case detail',
    topUp: 'https://openrouter.ai/credits',
  },
  brightdata: {
    label: 'Bright Data',
    funds: 'Finding channels — keyword sweeps, billed per record',
    topUp: 'https://brightdata.com/cp/billing',
  },
};

function usd(value: number): string {
  return `$${value.toFixed(2)}`;
}

/**
 * A balance is only ever shown with where it came from. `derived` means the
 * provider would not report it and the number is this harness's own spend
 * ledger subtracted from a configured starting figure — useful, but not the
 * same claim as a live reading, and the badge has to say so.
 */
function SourceBadge({ source }: { source: ProviderBalance['source'] }) {
  const style =
    source === 'live'
      ? 'border-[var(--grade-strong)]/40 text-[var(--grade-strong)]'
      : source === 'derived'
        ? 'border-[var(--track-keyword)]/40 text-[var(--track-keyword)]'
        : 'border-line text-ink-3';
  const label =
    source === 'live' ? 'Live' : source === 'derived' ? 'Estimated' : 'Unavailable';
  return (
    <span className={`rounded-full border px-2 py-0.5 text-xs ${style}`}>{label}</span>
  );
}

function ProviderCard({ balance }: { balance: ProviderBalance }) {
  const meta = PROVIDER_META[balance.provider] ?? {
    label: balance.provider,
    funds: '',
    topUp: '',
  };

  const spentPct =
    balance.limit_usd && balance.limit_usd > 0 && balance.spent_usd != null
      ? Math.min(100, (balance.spent_usd / balance.limit_usd) * 100)
      : null;

  return (
    <Panel>
      <div className="p-5">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="font-medium text-ink">{meta.label}</h2>
            <p className="mt-0.5 text-xs text-ink-2">{meta.funds}</p>
          </div>
          <SourceBadge source={balance.source} />
        </div>

        <p className="mt-4 text-3xl font-medium tabular-nums text-ink">
          {balance.available_usd != null ? usd(balance.available_usd) : '—'}
          <span className="ml-2 text-sm font-normal text-ink-3">available</span>
        </p>

        {spentPct != null && (
          <div className="mt-3">
            <div
              className="h-1.5 overflow-hidden rounded-full bg-sunken"
              role="img"
              aria-label={`${spentPct.toFixed(0)} percent of the allowance spent`}
            >
              <div
                className="h-full rounded-full bg-[var(--focus)]"
                style={{ width: `${spentPct}%` }}
              />
            </div>
            <p className="mt-1.5 text-xs tabular-nums text-ink-3">
              {usd(balance.spent_usd ?? 0)} spent of {usd(balance.limit_usd ?? 0)}
            </p>
          </div>
        )}

        {balance.detail && (
          <p className="mt-3 text-xs leading-snug text-ink-3">{balance.detail}</p>
        )}

        {balance.remediation && (
          <p className="mt-3 rounded border border-line bg-sunken px-3 py-2 text-xs leading-snug text-ink-2">
            {balance.remediation}
          </p>
        )}

        {meta.topUp && (
          <a
            href={meta.topUp}
            target="_blank"
            rel="noreferrer"
            className="mt-4 inline-block text-xs text-[var(--focus)] hover:underline"
          >
            Top up {meta.label} →
          </a>
        )}
      </div>
    </Panel>
  );
}

/** What the current wallet can and cannot buy — the same gate the launcher uses. */
function AffordabilityTable({ tiers }: { tiers: DepthTier[] }) {
  return (
    <Panel>
      <PanelHeader
        title="What you can run right now"
        hint="The same check we apply when a run starts"
      />
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs text-ink-3">
            <th className="px-4 py-2 font-normal">Depth</th>
            <th className="px-4 py-2 text-right font-normal">Discovery</th>
            <th className="px-4 py-2 text-right font-normal">Model</th>
            <th className="px-4 py-2 text-right font-normal">Total</th>
            <th className="px-4 py-2 font-normal">Status</th>
          </tr>
        </thead>
        <tbody>
          {tiers.map((tier) => (
            <tr key={tier.id} className="border-b border-line last:border-0">
              <td className="px-4 py-2.5">
                <span className="text-ink">{tier.label}</span>
                <span className="ml-2 text-xs text-ink-3">{tier.hours}h</span>
              </td>
              <td className="px-4 py-2.5 text-right tabular-nums text-ink-2">
                {usd(tier.est_brightdata_usd)}
              </td>
              <td className="px-4 py-2.5 text-right tabular-nums text-ink-2">
                {usd(tier.est_openrouter_usd)}
              </td>
              <td className="px-4 py-2.5 text-right font-medium tabular-nums text-ink">
                {usd(tier.est_total_usd)}
              </td>
              <td className="px-4 py-2.5">
                {tier.locked ? (
                  <span className="text-xs text-ink-3" title={tier.blockers.join(' ')}>
                    Needs more credit
                  </span>
                ) : (
                  <span className="text-xs text-[var(--grade-strong)]">Ready to run</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

export function BalancesPage() {
  const queryClient = useQueryClient();
  const balances = useQuery({
    queryKey: ['balances'],
    queryFn: () => api.balances(),
    refetchInterval: 60_000,
  });
  const depths = useQuery({ queryKey: ['depths'], queryFn: api.depths });

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <Eyebrow>Wallet</Eyebrow>
          <h1 className="mt-1 text-2xl font-medium text-ink">Provider balances</h1>
          <p className="mt-1 max-w-2xl text-sm text-ink-2">
            Finding channels and analysing them are billed to different
            providers, so they top up separately. If either runs low,
            we&rsquo;ll show you which depths it can still cover.
          </p>
        </div>
        <button
          type="button"
          onClick={async () => {
            await api.balances(true);
            queryClient.invalidateQueries({ queryKey: ['balances'] });
            queryClient.invalidateQueries({ queryKey: ['depths'] });
          }}
          className="rounded-md border border-line bg-raised px-3 py-1.5 text-sm text-ink-2 hover:text-ink"
        >
          Refresh
        </button>
      </header>

      {balances.isLoading && <Skeleton rows={3} />}
      {balances.isError && (
        <ErrorState
          title="Could not read balances"
          detail={(balances.error as Error)?.message}
        />
      )}

      {balances.data && (
        <div className="grid gap-4 sm:grid-cols-2">
          {balances.data.providers.map((p) => (
            <ProviderCard key={p.provider} balance={p} />
          ))}
        </div>
      )}

      {depths.data && <AffordabilityTable tiers={depths.data} />}
    </div>
  );
}
