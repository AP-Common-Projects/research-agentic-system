import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError, type DepthTier } from '../lib/api';
import { Panel, PanelHeader, Eyebrow, ErrorState, Skeleton } from '../components/primitives';

// The preview is a taste of the model's read on a topic, not its scope.
const PREVIEW_LIMIT = 10;

function usd(value: number): string {
  return `$${value.toFixed(2)}`;
}

/** One depth option. Locked tiers stay visible and explain themselves. */
function DepthCard({
  tier,
  selected,
  onSelect,
}: {
  tier: DepthTier;
  selected: boolean;
  onSelect: () => void;
}) {
  const locked = tier.locked;
  return (
    <button
      type="button"
      disabled={locked}
      onClick={onSelect}
      aria-pressed={selected}
      className={`relative flex h-full flex-col rounded-lg border p-4 text-left transition-all ${
        locked
          ? 'cursor-not-allowed border-line bg-sunken opacity-60'
          : selected
            ? 'border-[var(--focus)] bg-raised shadow-[0_0_0_1px_var(--focus)]'
            : 'border-line bg-raised hover:border-[var(--focus)]/50 hover:shadow-[var(--shadow)]'
      }`}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-medium text-ink">{tier.label}</span>
        <span className="rounded bg-sunken px-1.5 py-0.5 text-xs tabular-nums text-ink-2">
          {tier.hours}h
        </span>
      </div>

      <p className="mt-1.5 text-xs leading-snug text-ink-2">{tier.tagline}</p>

      <dl className="mt-3 space-y-1 text-xs">
        <div className="flex justify-between gap-2">
          <dt className="text-ink-3">Channels</dt>
          <dd className="tabular-nums text-ink-2">{tier.est_channels}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-ink-3">Videos</dt>
          <dd className="tabular-nums text-ink-2">{tier.est_videos}</dd>
        </div>
        <div className="flex justify-between gap-2 border-t border-line pt-1">
          <dt className="text-ink-3">Est. cost</dt>
          <dd className="font-medium tabular-nums text-ink">{usd(tier.est_total_usd)}</dd>
        </div>
      </dl>

      {locked && (
        <p className="mt-3 rounded border border-line bg-sunken px-2 py-1.5 text-xs leading-snug text-ink-2">
          <span className="font-medium text-ink">Locked.</span> {tier.blockers[0]}
        </p>
      )}
    </button>
  );
}

export function NewRunPage() {
  const queryClient = useQueryClient();

  const [topic, setTopic] = useState('');
  const [submittedTopic, setSubmittedTopic] = useState('');
  const [depth, setDepth] = useState<string | null>(null);
  const [launched, setLaunched] = useState<string | null>(null);

  const topics = useQuery({ queryKey: ['topics'], queryFn: api.topics });
  const depths = useQuery({
    queryKey: ['depths'],
    queryFn: api.depths,
    // The wallet moves while this page is open; a stale reading here is how
    // someone picks a tier that is no longer affordable.
    refetchInterval: 60_000,
  });

  // Shown, not chosen. Working out which sub-niches are worth covering is the
  // model's job -- both here as a preview and again inside the run, where the
  // taxonomy step expands the topic for real. Presenting it as a checklist
  // made the client responsible for the part they are paying us to do.
  const preview = useQuery({
    queryKey: ['subniches', submittedTopic],
    queryFn: () => api.suggestSubNiches(submittedTopic),
    enabled: submittedTopic.length >= 2,
    staleTime: 5 * 60_000,
  });

  const launch = useMutation({
    mutationFn: () => api.launchRun([submittedTopic], depth ?? undefined),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ['runs'] });
      setLaunched(run.run_id);
    },
  });

  const chosenTier = useMemo(
    () => depths.data?.find((t) => t.id === depth) ?? null,
    [depths.data, depth],
  );

  function submitTopic(value: string) {
    const next = value.trim();
    if (next.length < 2) return;
    setTopic(next);
    setSubmittedTopic(next);
    setLaunched(null);
  }

  const canLaunch = !!submittedTopic && depth != null && !launch.isPending;

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-6">
      <header>
        <Eyebrow>New research run</Eyebrow>
        <h1 className="mt-1 text-2xl font-medium text-ink">
          What should the harness map?
        </h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-2">
          Choose a topic and how deep to go. Finding the sub-niches worth
          covering is the model&rsquo;s job, not yours. Depth options price
          themselves against the live wallet and lock when the balance cannot
          fund them.
        </p>
      </header>

      {/* ---- 1. Topic ---- */}
      <Panel>
        <PanelHeader title="1 · Topic" hint="Pick a covered topic, or write your own" />
        <div className="space-y-4 p-4">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              submitTopic(topic);
            }}
            className="flex gap-2"
          >
            <input
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="e.g. personal finance, true crime, home fitness…"
              aria-label="Topic"
              className="flex-1 rounded-md border border-line bg-raised px-3 py-2 text-sm text-ink outline-none placeholder:text-ink-3 focus:border-[var(--focus)]"
            />
            <button
              type="submit"
              disabled={topic.trim().length < 2}
              className="rounded-md bg-[var(--focus)] px-4 py-2 text-sm font-medium text-white transition-opacity disabled:opacity-40"
            >
              Use this topic
            </button>
          </form>

          {topics.isLoading && <Skeleton rows={1} />}
          {topics.data && topics.data.length > 0 && (
            <div>
              <p className="mb-2 text-xs text-ink-3">
                Topics already covered by the dataset
              </p>
              <div className="flex flex-wrap gap-1.5">
                {topics.data.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => submitTopic(t.id)}
                    className={`rounded-full border px-3 py-1.5 text-sm transition-colors ${
                      submittedTopic === t.id
                        ? 'border-[var(--focus)] bg-raised text-ink'
                        : 'border-line bg-raised text-ink-2 hover:border-[var(--focus)] hover:text-ink'
                    }`}
                  >
                    {t.label}
                    <span className="ml-1.5 tabular-nums text-xs text-ink-3">
                      {t.channel_count.toLocaleString()}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </Panel>

      {/* ---- What the model intends to cover (read-only) ---- */}
      {submittedTopic && (
        <Panel>
          <PanelHeader
            title="What the model will cover"
            hint={
              preview.data?.source === 'dataset'
                ? 'Areas already in the dataset for this topic'
                : 'Proposed by the model — the run confirms or replaces them'
            }
          />
          <div className="space-y-3 p-4">
            {preview.isLoading && <Skeleton rows={2} />}
            {preview.isError && (
              <p className="text-sm text-ink-2">
                The preview could not be generated, which does not block the
                run — the harness works the sub-niches out for itself once it
                starts.
              </p>
            )}
            {preview.data && (
              <>
                <div className="flex flex-wrap items-center gap-1.5">
                  {/* Ten is a sample, not the scope. The run explores far more
                      than this, so the trailing "etc…" is load-bearing: a
                      closed list of ten would read as the whole plan. */}
                  {preview.data.subniches.slice(0, PREVIEW_LIMIT).map((s) => (
                    <span
                      key={s.slug}
                      title={s.rationale}
                      className="flex items-center gap-2 rounded-full border border-line bg-raised px-3 py-1.5 text-sm text-ink-2"
                    >
                      <span
                        aria-hidden
                        className="size-1.5 rounded-full"
                        style={{
                          background:
                            s.source === 'dataset'
                              ? 'var(--track-graph)'
                              : 'var(--track-keyword)',
                        }}
                      />
                      {s.name}
                      {s.channel_count != null && (
                        <span className="tabular-nums text-xs text-ink-3">
                          {s.channel_count}
                        </span>
                      )}
                    </span>
                  ))}
                  <span className="px-1 text-sm text-ink-3">etc…</span>
                </div>
                <p className="border-t border-line pt-3 text-xs leading-snug text-ink-3">
                  A preview, not a plan you have to approve. The run expands
                  the topic itself and will follow whatever it finds, including
                  areas not listed here.
                </p>
              </>
            )}
          </div>
        </Panel>
      )}

      {/* ---- 2. Depth ---- */}
      {submittedTopic && (
        <Panel>
          <PanelHeader title="2 · Depth" hint="How far the run goes before it stops" />
          <div className="p-4">
            {depths.isLoading && <Skeleton rows={3} />}
            {depths.isError && (
              <ErrorState
                title="Could not load depth options"
                detail={(depths.error as Error)?.message}
              />
            )}
            {depths.data && (
              <>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {depths.data.map((tier) => (
                    <DepthCard
                      key={tier.id}
                      tier={tier}
                      selected={depth === tier.id}
                      onSelect={() => setDepth(tier.id)}
                    />
                  ))}
                </div>
                {depths.data.some((t) => t.warnings.length > 0) && (
                  <p className="mt-3 rounded border border-line bg-sunken px-3 py-2 text-xs leading-snug text-ink-2">
                    {depths.data.find((t) => t.warnings.length > 0)?.warnings[0]}
                  </p>
                )}
              </>
            )}
          </div>
        </Panel>
      )}

      {/* ---- Launch ---- */}
      {chosenTier && (
        <Panel>
          <div className="flex flex-wrap items-center justify-between gap-4 p-4">
            <div className="text-sm">
              <p className="text-ink">
                <span className="font-medium">{submittedTopic}</span> ·{' '}
                {chosenTier.label} · {chosenTier.hours}h
              </p>
              <p className="mt-0.5 text-xs text-ink-2">
                Estimated {usd(chosenTier.est_total_usd)} —{' '}
                {usd(chosenTier.est_brightdata_usd)} discovery,{' '}
                {usd(chosenTier.est_openrouter_usd)} model
              </p>
            </div>
            <button
              type="button"
              disabled={!canLaunch}
              onClick={() => launch.mutate()}
              className="rounded-md bg-[var(--focus)] px-5 py-2.5 text-sm font-medium text-white transition-opacity disabled:opacity-40"
            >
              {launch.isPending ? 'Starting…' : 'Start run'}
            </button>
          </div>

          {launched && (
            <div className="border-t border-line p-4">
              <p className="text-sm text-ink">
                Run started —{' '}
                <span className="font-mono text-xs text-ink-2">{launched}</span>
              </p>
              <p className="mt-1 text-xs text-ink-2">
                It runs detached, so you can close this page. The workbook
                appears under Workbooks when it finishes.
              </p>
            </div>
          )}

          {launch.isError && (
            <div className="border-t border-line p-4">
              <ErrorState
                title={
                  (launch.error as ApiError)?.status === 409
                    ? 'Balance changed — this depth is no longer affordable'
                    : 'Could not start the run'
                }
                detail={(launch.error as Error)?.message}
              />
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}
