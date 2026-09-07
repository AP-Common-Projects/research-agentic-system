import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError, type DepthTier } from '../lib/api';
import {
  Panel,
  PanelHeader,
  Eyebrow,
  ErrorState,
  Skeleton,
  Tooltip,
} from '../components/primitives';
import { duration } from '../lib/format';

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
          {duration(tier.duration_label, tier.hours)}
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

      {/* Every blocker, not just the first. A run spends at two providers,
          and a depth can be short at both — showing only the leading one
          let a top-up clear the message while the depth stayed locked. */}
      {locked && (
        <div className="mt-3 space-y-1 rounded border border-line bg-sunken px-2 py-1.5 text-xs leading-snug text-ink-2">
          <p>
            <span className="font-medium text-ink">Not available yet.</span>{' '}
            {tier.blockers[0]}
          </p>
          {tier.blockers.slice(1).map((b) => (
            <p key={b}>{b}</p>
          ))}
        </div>
      )}

      {/* A balance we could not read does not lock a depth — the harness has
          its own circuit breaker — but the client should know the estimate
          went unchecked rather than assume it passed. */}
      {!locked && tier.warnings.length > 0 && (
        <div className="mt-3 space-y-1 rounded border border-dashed border-line px-2 py-1.5 text-xs leading-snug text-ink-3">
          {tier.warnings.map((w) => (
            <p key={w}>{w}</p>
          ))}
        </div>
      )}
    </button>
  );
}

/* --------------------------------------------------------------------------
 * Thresholds
 *
 * The depth tier decides how long a run goes and how many channels it can
 * finish. These decide what counts as worth including at all -- and they
 * used to live in .env, which made "only channels above 50k subscribers" a
 * property of the deployment rather than of the question being asked.
 *
 * Collapsed by default. Every one of them has a defensible default, and a
 * client who has not thought about the subscriber floor should not have to
 * decide about it before they can start a run.
 * ----------------------------------------------------------------------- */

function ThresholdsPanel({
  depth,
  value,
  onChange,
}: {
  depth: string;
  value: Record<string, number>;
  onChange: (next: Record<string, number>) => void;
}) {
  const [open, setOpen] = useState(false);
  const thresholds = useQuery({
    queryKey: ['thresholds', depth],
    queryFn: () => api.thresholds(depth),
  });

  const changed = Object.keys(value).length;

  return (
    <Panel>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-4 px-4 py-3 text-left"
      >
        <div>
          <h2 className="font-display text-sm font-semibold tracking-tight text-ink">
            3 · Thresholds
          </h2>
          <p className="mt-0.5 text-xs text-ink-3">
            {changed > 0
              ? `${changed} changed from the default`
              : 'Optional — sensible defaults are already set'}
          </p>
        </div>
        <span aria-hidden className="text-xs text-ink-3">
          {open ? 'Hide' : 'Adjust'}
        </span>
      </button>

      {open && (
        <div className="space-y-4 border-t border-line p-4">
          {thresholds.isLoading && <Skeleton rows={3} />}
          {thresholds.isError && (
            <p className="text-sm text-ink-2">
              Couldn&rsquo;t load the thresholds — the run will use its
              defaults, which is what it would have done anyway.
            </p>
          )}
          {thresholds.data?.map((t) => {
            const current = value[t.id] ?? t.default ?? t.minimum;
            const isChanged = t.id in value;
            return (
              <div key={t.id}>
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <label htmlFor={`th-${t.id}`} className="text-sm text-ink">
                    {t.label}
                  </label>
                  <span className="font-mono text-xs tabular-nums text-ink-2">
                    {t.kind === 'int'
                      ? Number(current).toLocaleString()
                      : Number(current)}
                    {t.unit ? ` ${t.unit}` : ''}
                    {isChanged && (
                      <button
                        type="button"
                        onClick={() => {
                          const next = { ...value };
                          delete next[t.id];
                          onChange(next);
                        }}
                        className="ml-2 text-[10px] text-[var(--focus)] hover:underline"
                      >
                        reset
                      </button>
                    )}
                  </span>
                </div>
                <input
                  id={`th-${t.id}`}
                  type="range"
                  min={t.minimum}
                  max={t.maximum}
                  step={t.step}
                  value={current}
                  onChange={(e) =>
                    onChange({ ...value, [t.id]: Number(e.target.value) })
                  }
                  className="mt-2 w-full accent-[var(--focus)]"
                />
                <p className="mt-1 text-xs leading-snug text-ink-3">{t.help}</p>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

export function NewRunPage() {
  const queryClient = useQueryClient();

  const [topic, setTopic] = useState('');
  const [submittedTopic, setSubmittedTopic] = useState('');
  const [depth, setDepth] = useState<string | null>(null);
  // Only ids the reader actually changed. Sending the untouched defaults
  // back would pin them, so a later change to a default would not reach a
  // run the client thought they had left alone.
  const [thresholds, setThresholds] = useState<Record<string, number>>({});
  const [launched, setLaunched] = useState<string | null>(null);

  const topics = useQuery({ queryKey: ['topics'], queryFn: api.topics });
  // Keyed on the topic as well as nothing else: the same depth is not the
  // same run on every vertical. Crime classifies every video into its
  // case-file columns, so an hour buys roughly a third of the channels and
  // costs more, and the cards have to say so before the client picks one.
  const depths = useQuery({
    queryKey: ['depths', submittedTopic],
    queryFn: () => api.depths(submittedTopic ?? undefined),
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
    mutationFn: () =>
      api.launchRun([submittedTopic], depth ?? undefined, thresholds),
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

  // Typing and picking from the list both land in `topic`; the button is the
  // only thing that commits it. Clicking a chip used to commit on its own,
  // which left the button writing a value it already held -- React drops an
  // identical state write, so it read as a dead button in that path alone.
  const isCommitted = topic.trim().length >= 2 && topic.trim() === submittedTopic;
  const canSubmitTopic = topic.trim().length >= 2 && !isCommitted;

  // The input can be edited past what was committed, so the preview names the
  // topic it actually describes rather than whatever is currently typed.
  const committedLabel =
    topics.data?.find((t) => t.id === submittedTopic)?.label ?? submittedTopic;

  const canLaunch = !!submittedTopic && depth != null && !launch.isPending;

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-6">
      <header>
        <Eyebrow>New research run</Eyebrow>
        <h1 className="mt-1 text-2xl font-medium text-ink">
          What would you like researched?
        </h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-2">
          Tell us the topic and how deep you&rsquo;d like to go — the model
          works out which sub-niches are worth covering from there. Each depth
          shows what it&rsquo;s likely to cost, and we&rsquo;ll flag any your
          current balance won&rsquo;t stretch to.
        </p>
      </header>

      {/* ---- 1. Topic ---- */}
      <Panel>
        <PanelHeader title="1 · Topic" hint="Pick one we already cover, or describe your own, then confirm" />
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
              disabled={!canSubmitTopic}
              className="rounded-md bg-[var(--focus)] px-4 py-2 text-sm font-medium text-white transition-opacity disabled:opacity-40"
            >
              {isCommitted ? 'Topic set' : 'Use this topic'}
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
                    onClick={() => setTopic(t.id)}
                    aria-pressed={topic.trim() === t.id}
                    className={`rounded-full border px-3 py-1.5 text-sm transition-colors ${
                      topic.trim() === t.id
                        ? 'border-[var(--focus)] bg-raised text-ink'
                        : 'border-line bg-raised text-ink-2 hover:border-[var(--focus)] hover:text-ink'
                    }`}
                  >
                    {t.label}
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
            title={`What the model will cover for \u201c${committedLabel}\u201d`}
            hint={
              preview.data?.source === 'dataset'
                ? 'Areas we already have channels for'
                : 'The model\u2019s first read — the run refines it'
            }
          />
          <div className="space-y-3 p-4">
            {preview.isLoading && <Skeleton rows={2} />}
            {preview.isError && (
              <p className="text-sm text-ink-2">
                Couldn&rsquo;t generate a preview just now — that
                won&rsquo;t hold anything up. The run works the sub-niches out
                for itself once it starts.
              </p>
            )}
            {preview.data && (
              <>
                <div className="flex flex-wrap items-center gap-1.5">
                  {/* Ten is a sample, not the scope. The run explores far more
                      than this, so the trailing "etc…" is load-bearing: a
                      closed list of ten would read as the whole plan. */}
                  {preview.data.subniches.slice(0, PREVIEW_LIMIT).map((s) => (
                    // Dataset rows carry no rationale -- the only thing there
                    // was to say was the channel count, which this page
                    // deliberately does not show, so no tooltip appears.
                    <Tooltip key={s.slug} label={s.rationale || undefined}>
                      <span className="flex items-center gap-2 rounded-full border border-line bg-raised px-3 py-1.5 text-sm text-ink-2">
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
                      </span>
                    </Tooltip>
                  ))}
                  <span className="px-1 text-sm text-ink-3">etc…</span>
                </div>
                <p className="border-t border-line pt-3 text-xs leading-snug text-ink-3">
                  Just a preview — nothing to approve. Once running, it
                  explores the topic properly and follows whatever it turns up,
                  including areas not shown here.
                </p>
              </>
            )}
          </div>
        </Panel>
      )}

      {/* ---- 2. Depth ---- */}
      {submittedTopic && (
        <Panel>
          <PanelHeader title="2 · Depth" hint="How much ground the run covers" />
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
                {/* Said once, above the cards, rather than repeated on each:
                    otherwise the smaller channel counts read as a mistake. */}
                {depths.data.some((t) => t.crime) && (
                  <p className="mb-3 rounded border border-line bg-sunken px-3 py-2 text-xs leading-snug text-ink-2">
                    <span className="font-medium text-ink">
                      Crime runs are sized differently.
                    </span>{' '}
                    Every video is also classified into the case-file columns
                    &mdash; crime type, victim, case status, what footage it
                    carries &mdash; which costs about four seconds a video on
                    top of the usual work. The same hours therefore cover fewer
                    channels, and cost more, than they would on another topic.
                  </p>
                )}
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

      {/* ---- 3. Thresholds (optional) ---- */}
      {chosenTier && <ThresholdsPanel depth={chosenTier.id} value={thresholds} onChange={setThresholds} />}

      {/* ---- Launch ---- */}
      {chosenTier && (
        <Panel>
          <div className="flex flex-wrap items-center justify-between gap-4 p-4">
            <div className="text-sm">
              <p className="text-ink">
                <span className="font-medium">{submittedTopic}</span> ·{' '}
                {chosenTier.label} · {duration(chosenTier.duration_label, chosenTier.hours)}
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
                Feel free to close this page — it keeps running. The
                workbook will appear under Workbooks when it&rsquo;s done.
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
