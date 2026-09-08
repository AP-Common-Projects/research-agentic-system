import type { NodeLogEntry } from './api';

/* --------------------------------------------------------------------------
 * How far along a run is.
 *
 * There is no progress number in the harness to read -- a run is a graph
 * that loops, not a queue that drains -- so this derives one from which
 * PHASE the last logged node belongs to. Phases are ordered and weighted by
 * the wall clock they actually cost, measured on a real Glimpse run:
 *
 *   plan       19s     taxonomy and niche selection
 *   discover  861s     the Bright Data fan-out
 *   enrich   1320s     hydration, then per-channel signals and classification
 *   assess     30s     saturation and branch compaction
 *   finish      -      the write-up chain and export
 *
 * Weighting by duration rather than by node count is the point: counting
 * nodes puts the bar at 50% after nineteen seconds of planning, because
 * planning is three of the six nodes and none of the time.
 *
 * A run can revisit a phase -- the graph loops over branches -- so progress
 * is monotonic by construction: it never goes backwards on a new branch.
 *
 * Phases alone are not enough, and assuming they were made the bar useless
 * on the deeper tiers. Taking the furthest phase EVER reached, one single
 * discovery round touches assess and puts the bar at 92%. Sample is one
 * round, so that was near enough. Standard is sixteen and Deep is
 * sixty-four: a Standard education run sat at 98% two hours into a
 * five-hour budget, with fifteen of its sixteen rounds still to come.
 *
 * So the research phases share their weight across the rounds the tier
 * budgets for, and the phase within the current round only says how far
 * into THAT round the run is. `roundsTotal` comes from the run itself --
 * branches x rounds, recorded at launch -- rather than being reconstructed
 * here from governors the console would then have to keep in step.
 *
 * A run that saturates early is not at 40% forever: reaching the write-up
 * chain means research is over however many rounds it used, and the bar
 * says so.
 * ----------------------------------------------------------------------- */

export type Phase = 'plan' | 'discover' | 'enrich' | 'assess' | 'finish';

export const PHASE_ORDER: Phase[] = ['plan', 'discover', 'enrich', 'assess', 'finish'];

export const PHASE_LABEL: Record<Phase, string> = {
  plan: 'Planning',
  discover: 'Finding channels',
  enrich: 'Analysing channels',
  assess: 'Assessing coverage',
  finish: 'Writing the workbook',
};

export const PHASE_DETAIL: Record<Phase, string> = {
  plan: 'Reading the topic and mapping its sub-niches',
  discover: 'Keyword sweeps and following channel references',
  enrich: 'Pulling channel details, then classifying each one',
  assess: 'Checking whether the topic is exhausted',
  finish: 'Success factors, cohorts, and the Excel export',
};

/** Share of a run's wall clock, from the measured timings above. */
const PHASE_WEIGHT: Record<Phase, number> = {
  plan: 0.02,
  discover: 0.34,
  enrich: 0.55,
  assess: 0.03,
  finish: 0.06,
};

const NODE_PHASE: Record<string, Phase> = {
  scan_niches: 'plan',
  expand_niche_adjacency: 'plan',
  build_taxonomy: 'plan',
  select_next_node: 'plan',
  underperformer_discovery: 'discover',
  keyword_search: 'discover',
  breakout_scanner: 'discover',
  graph_walk: 'discover',
  new_channel_discovery: 'discover',
  hydrate_metadata: 'enrich',
  resolve_geo_language: 'enrich',
  extract_metadata_signals: 'enrich',
  score_signals: 'enrich',
  classify_channel: 'enrich',
  resolve_first_video_date: 'enrich',
  check_saturation: 'assess',
  cluster_branch: 'assess',
  compact_branch: 'assess',
  extract_success_failure_factors: 'finish',
  describe_video_titles: 'finish',
  populate_taxonomy_dimensions: 'finish',
  populate_crime_metadata: 'finish',
  populate_shared_fields: 'finish',
  assign_cohorts: 'finish',
  finalize_dataset: 'finish',
  synthesize: 'finish',
};

export function phaseOf(nodeName: string): Phase {
  return NODE_PHASE[nodeName] ?? 'plan';
}

export interface RunProgress {
  /** 0-100. A finished run is always 100, whatever it logged. */
  percent: number;
  phase: Phase;
  /** Phases still ahead, in order — "what happens next". */
  remaining: Phase[];
  /** Phases fully behind it. */
  done: Phase[];
}

/** The research phases' combined share — everything before the write-up. */
const RESEARCH_SHARE =
  PHASE_WEIGHT.plan + PHASE_WEIGHT.discover + PHASE_WEIGHT.enrich + PHASE_WEIGHT.assess;

/** How far through a single round each phase sits, by its own weight. */
const WITHIN_ROUND: Record<Phase, number> = {
  plan: 0,
  discover: PHASE_WEIGHT.plan / RESEARCH_SHARE,
  enrich: (PHASE_WEIGHT.plan + PHASE_WEIGHT.discover) / RESEARCH_SHARE,
  assess:
    (PHASE_WEIGHT.plan + PHASE_WEIGHT.discover + PHASE_WEIGHT.enrich) /
    RESEARCH_SHARE,
  finish: 1,
};

/** Nodes of the write-up chain, in the order they run. */
const FINISH_NODES = [
  'extract_success_failure_factors',
  'describe_video_titles',
  'populate_taxonomy_dimensions',
  'populate_crime_metadata',
  'populate_shared_fields',
  'assign_cohorts',
  'finalize_dataset',
  'synthesize',
];

export function computeProgress(
  entries: NodeLogEntry[],
  status: string,
  roundsTotal?: number | null,
): RunProgress {
  const finished = status === 'complete' || status === 'stopped';

  if (entries.length === 0) {
    return {
      percent: finished ? 100 : 0,
      phase: 'plan',
      remaining: PHASE_ORDER.slice(1),
      done: [],
    };
  }

  // Furthest reached, not most recent: the graph loops back to discovery
  // for each new branch, and a bar that retreats reads as a failure.
  let furthest = 0;
  for (const e of entries) {
    const idx = PHASE_ORDER.indexOf(phaseOf(e.node_name));
    if (idx > furthest) furthest = idx;
  }
  const phase = PHASE_ORDER[furthest];

  // One check_saturation per discovery round: it is the node that closes a
  // round, so counting it counts rounds without inferring them from the
  // shape of the loop.
  const roundsDone = entries.filter(
    (e) => e.node_name === 'check_saturation',
  ).length;

  // A run whose depth is unknown (a bare CLI run has no tier) falls back to
  // one round, which is the old behaviour rather than a guess.
  const total = Math.max(1, roundsTotal ?? 1);

  const seen = new Set(entries.map((e) => e.node_name));
  const finishDone = FINISH_NODES.filter((n) => seen.has(n)).length;
  const inWriteUp = finishDone > 0;

  let percent: number;
  if (inWriteUp) {
    // Research is over however many rounds it used -- an early saturation
    // is a finished search, not an abandoned one.
    percent =
      RESEARCH_SHARE + PHASE_WEIGHT.finish * (finishDone / FINISH_NODES.length);
  } else {
    const within = WITHIN_ROUND[phase];
    percent = RESEARCH_SHARE * Math.min(1, (roundsDone + within) / total);
  }

  return {
    percent: finished ? 100 : Math.min(99, Math.round(percent * 100)),
    phase,
    remaining: PHASE_ORDER.slice(furthest + 1),
    done: PHASE_ORDER.slice(0, furthest),
  };
}
