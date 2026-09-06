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
 * is monotonic by construction: it takes the furthest phase reached, never
 * the current one, or the bar would slide backwards on the second branch.
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

export function computeProgress(
  entries: NodeLogEntry[],
  status: string,
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

  // Everything before the current phase is complete; the current phase
  // counts as half, since nothing here knows how far into it the run is.
  let percent = 0;
  for (let i = 0; i < furthest; i++) percent += PHASE_WEIGHT[PHASE_ORDER[i]];
  percent += PHASE_WEIGHT[phase] / 2;

  return {
    percent: finished ? 100 : Math.min(99, Math.round(percent * 100)),
    phase,
    remaining: PHASE_ORDER.slice(furthest + 1),
    done: PHASE_ORDER.slice(0, furthest),
  };
}
