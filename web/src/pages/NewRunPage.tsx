import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { api, ApiError, type DepthTier, type SubNiche } from '../lib/api';
import { Panel, PanelHeader, Eyebrow, ErrorState, Skeleton } from '../components/primitives';

/** Money, at the precision a person reading a wallet actually wants. */
function usd(value: number): string {
  return `$${value.toFixed(2)}`;
}

/**
 * Sub-niche chip. A dataset-backed suggestion carries a real channel count
 * and is styled as evidence; a model-proposed one is visibly lighter, because
 * presenting a guess with the same authority as a measurement is how someone
 * picks a sub-niche that turns out to have four channels in it.
 */
function SubNicheChip({
  item,
  selected,
  onToggle,
}: {
  item: SubNiche;
  selected: boolean;
  onToggle: () => void;
}) {
  const measured = item.source === 'dataset';
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={selected}
      title={item.rationale}
      className={`group flex items-center gap-2 rounded-full border px-3 py-1.5 text-left text-sm transition-colors ${
        selected
          ? 'border-transparent bg-[var(--focus)] text-white'
          : 'border-line bg-raised text-ink-2 hover:border-[var(--focus)] hover:text-ink'
      }`}
    >
      <span
        aria-hidden
        className={`size-1.5 rounded-full ${
          selected
            ? 'bg-white/80'
            : measured
              ? 'bg-[var(--track-graph)]'
              : 'bg-[var(--track-keyword)]'
        }`}
      />
      <span>{item.name}</span>
      {measured && item.channel_count != null && (
        <span
          className={`tabular-nums text-xs ${selected ? 'text-white/75' : 'text-ink-3'}`}
        >
          {item.channel_count}
        </span>
      )}
    </button>
  );
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
          <span className="font-medium text-ink">Locked.</span>{' '}
          {tier.blockers[0]}
        </p>
      )}
    </button>
  );
}

export function NewRunPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [topic, setTopic] = useState('');
  const [submittedTopic, setSubmittedTopic] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [depth, setDepth] = useState<string | null>(null);

  const topics = useQuery({ queryKey: ['topics'], queryFn: api.topics });
  const depths = useQuery({
    queryKey: ['depths'],
    queryFn: api.depths,
    // The wallet moves while this page is open; a stale reading here is how
    // someone picks a tier that is no longer affordable.
    refetchInterval: 60_000,
  });

  const suggestions = useQuery({
    queryKey: ['subniches', submittedTopic],
    queryFn: () => api.suggestSubNiches(submittedTopic),
    enabled: submittedTopic.length >= 2,
    staleTime: 5 * 60_000,
  });

  const launch = useMutation({
    mutationFn: () => api.launchRun(selected, depth ?? undefined),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ['runs'] });
      navigate(`/runs/${run.run_id}`);
    },
  });

  const chosenTier = useMemo(
    () => depths.data?.find((t) => t.id === depth) ?? null,
    [depths.data, depth],
  );

  function toggle(name: string) {
    setSelected((prev) =>
      prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name],
    );
  }

  function submitTopic(value: string) {
    const next = value.trim();
    if (next.length < 2) return;
    setTopic(next);
    setSubmittedTopic(next);
    setSelected([]);
  }

  const canLaunch = selected.length > 0 && depth != null && !launch.isPending;

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-6">
      <header>
        <Eyebrow>New research run</Eyebrow>
        <h1 className="mt-1 text-2xl font-medium text-ink">
          What should the harness map?
        </h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-2">
          Pick a topic, choose the sub-niches worth covering, then decide how
          deep to go. Depth options price themselves against the live wallet
          and lock when the balance cannot fund them.
        </p>
      </header>

      {/* ---- 1. Topic ---- */}
      <Panel>
        <PanelHeader title="1 · Topic" hint="Choose a covered topic, or write your own" />
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
              Find sub-niches
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

      {/* ---- 2. Sub-niches ---- */}
      {submittedTopic && (
        <Panel>
          <PanelHeader
            title="2 · Sub-niches"
            hint={
              suggestions.data?.source === 'dataset'
                ? 'Measured from the dataset — counts are real'
                : 'Proposed by the model — unverified until a run confirms them'
            }
          />
          <div className="space-y-3 p-4">
            {suggestions.isLoading && <Skeleton rows={2} />}
            {suggestions.isError && (
              <ErrorState
                title="Could not suggest sub-niches"
                detail={(suggestions.error as Error)?.message}
              />
            )}
            {suggestions.data && (
              <>
                {suggestions.data.note && (
                  <p className="text-xs leading-snug text-ink-3">
                    {suggestions.data.note}
                  </p>
                )}
                <div className="flex flex-wrap gap-1.5">
                  {suggestions.data.subniches.map((s) => (
                    <SubNicheChip
                      key={s.slug}
                      item={s}
                      selected={selected.includes(s.name)}
                      onToggle={() => toggle(s.name)}
                    />
                  ))}
                </div>
                {suggestions.data.subniches.length === 0 && (
                  <p className="text-sm text-ink-2">
                    Nothing came back for that topic. Try wording it the way a
                    viewer would describe the channels.
                  </p>
                )}
                <p className="border-t border-line pt-3 text-xs text-ink-3">
                  {selected.length} selected
                  {selected.length > 0 && ` — ${selected.join(', ')}`}
                </p>
              </>
            )}
          </div>
        </Panel>
      )}

      {/* ---- 3. Depth ---- */}
      {selected.length > 0 && (
        <Panel>
          <PanelHeader
            title="3 · Depth"
            hint="How far the run goes before it stops"
          />
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
                <span className="font-medium">{chosenTier.label}</span> ·{' '}
                {chosenTier.hours}h · {selected.length} sub-niche
                {selected.length === 1 ? '' : 's'}
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
