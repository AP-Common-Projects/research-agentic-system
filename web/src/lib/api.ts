/** Typed client for the console API. Mirrors src/api/server.py. */

const BASE = import.meta.env.DEV ? 'http://localhost:8000' : '';

// --- Wallet, depth, topics, workbooks -------------------------------------

export type BalanceSource = 'live' | 'derived' | 'unavailable';

export interface ProviderBalance {
  provider: 'openrouter' | 'brightdata';
  available_usd: number | null;
  source: BalanceSource;
  detail: string;
  spent_usd: number | null;
  limit_usd: number | null;
  remediation: string;
}

export interface Balances {
  providers: ProviderBalance[];
  checked_at: number;
}

export interface DepthTier {
  id: string;
  label: string;
  hours: number;
  /** Server-formatted: "30m", "1h", "72h". Absent on older servers. */
  duration_label?: string;
  /** Research window plus the completeness gate — what the client waits. */
  total_duration_label?: string;
  /** The gate's own share, e.g. "up to 27m". */
  gate_label?: string;
  tagline: string;
  description: string;
  est_channels: string;
  est_videos: string;
  est_brightdata_usd: number;
  est_openrouter_usd: number;
  est_total_usd: number;
  governors: Record<string, number>;
  locked: boolean;
  blockers: string[];
  warnings: string[];
  /** Crime classifies every video into its case-file columns, so the same
   *  hours buy fewer channels and cost more. Set by the server. */
  crime?: boolean;
}

export interface Threshold {
  id: string;
  env: string;
  label: string;
  help: string;
  kind: 'int' | 'float';
  minimum: number;
  maximum: number;
  step: number;
  unit: string;
  default: number | null;
}

export interface Topic {
  id: string;
  label: string;
  niche_count: number;
  channel_count: number;
  has_dataset: boolean;
}

export interface SubNiche {
  name: string;
  slug: string;
  channel_count: number | null;
  source: 'dataset' | 'proposed';
  rationale: string;
}

export interface SubNicheSuggestion {
  topic: string;
  source: 'dataset' | 'proposed' | 'none';
  note?: string;
  subniches: SubNiche[];
}

export interface TreeNodeData {
  id: string;
  name: string;
  kind: 'root' | 'family' | 'sub_niche' | 'channel';
  channel_count?: number;
  subscriber_count?: number;
  discovery_method?: string;
  children?: TreeNodeData[];
}

export interface WorkbookTree extends TreeNodeData {
  workbook_id: string;
}

export interface WorkbookSpendRun {
  run_id: string;
  model_usd: number;
  records: number;
  discovery_usd: number;
}

export interface WorkbookSpend {
  workbook_id: string;
  run_count: number;
  attributed_run_count: number;
  openrouter_usd: number;
  brightdata_usd: number;
  brightdata_records: number;
  total_usd: number;
  cost_per_record_usd: number;
  by_run: WorkbookSpendRun[];
  note: string;
}

export interface TreeNodeData {
  id: string;
  name: string;
  kind: 'root' | 'family' | 'sub_niche' | 'channel';
  channel_count?: number;
  subscriber_count?: number;
  discovery_method?: string;
  children?: TreeNodeData[];
}

export interface WorkbookTree extends TreeNodeData {
  workbook_id: string;
}

export interface WorkbookSpendRun {
  run_id: string;
  model_usd: number;
  records: number;
  discovery_usd: number;
}

export interface WorkbookSpend {
  workbook_id: string;
  run_count: number;
  attributed_run_count: number;
  openrouter_usd: number;
  brightdata_usd: number;
  brightdata_records: number;
  total_usd: number;
  cost_per_record_usd: number;
  by_run: WorkbookSpendRun[];
  note: string;
}

export interface WorkbookGraphNode {
  channel_id: string;
  title: string | null;
  subscriber_count: number | null;
  discovery_method: string | null;
  sub_niche: string | null;
}

export interface WorkbookGraphEdge {
  source: string;
  target: string;
  edge_type: string | null;
  internal: boolean;
}

export interface WorkbookGraph {
  workbook_id: string;
  nodes: WorkbookGraphNode[];
  edges: WorkbookGraphEdge[];
  channel_count: number;
  edge_count: number;
  internal_edge_count: number;
  by_track: Record<string, number>;
}

export interface WorkbookSheet {
  name: string;
  rows: number;
}

export interface Workbook {
  id: string;
  title: string;
  vertical: string;
  description: string;
  filename: string;
  available: boolean;
  download_url: string;
  size_bytes?: number;
  modified_at?: number;
  sheets?: WorkbookSheet[];
  channel_count?: number;
  video_count?: number;
  error?: string;
}


export type RunStatus = 'running' | 'complete' | 'stopped' | 'pending';

export interface Run {
  run_id: string;
  thread_id: string;
  niches: string[];
  pid: number | null;
  started_at: string;
  // Present on console-launched runs; absent on ones started from the CLI.
  depth?: string | null;
  depth_label?: string | null;
  depth_hours?: number | null;
  thresholds?: Record<string, number> | null;
  status: RunStatus;
  last_node: string | null;
  last_activity_at: string | null;
  log_lines: number;
  cost_usd: number;
}

export interface TreeNode {
  id: string;
  label: string;
  depth: number;
  parent_id: string | null;
  children_ids: string[];
  keywords: string[];
  status: 'pending' | 'active' | 'saturated' | 'compacted';
  saturated_at: string | null;
  _kw_novelty_history?: number[];
  _gw_novelty_history?: number[];
}

export type Grade = 'strong' | 'moderate' | 'weak';

export interface GradedFinding {
  claim: string;
  grade: Grade;
  pattern_type: string;
  supporting_channel_ids: string[];
  evidence: {
    corroboration?: Grade;
    consistency?: Grade;
    recency?: Grade;
    effect_size?: Grade;
    max_outlier_score?: number;
    [k: string]: unknown;
  };
}

export interface FinalReport {
  niche: string;
  run_id: string;
  generated_at: string;
  summary: string;
  findings: GradedFinding[];
  cannot_determine: string[];
  discovery_stats: Record<string, number | string>;
}

export interface ReportSummary {
  run_id: string;
  niche: string;
  generated_at: string;
  summary: string;
  finding_count: number;
  grade_counts: Record<Grade, number>;
  cannot_determine_count: number;
  discovery_stats: Record<string, number | string>;
}

export interface RunState {
  selected_niche: string;
  tree: Record<string, TreeNode>;
  active_node_id: string | null;
  budget_spent_usd: number;
  novelty_rates: number[];
  saturated_branches: string[];
  errors: { node_name: string; error_type: string; message: string; timestamp: string }[];
  final_report: FinalReport | null;
  schema_version: number | null;
  discovered_channel_count: number;
  discovered_video_count: number;
  keyword_channel_count: number;
  graph_walk_channel_count: number;
  graph_walk_exclusive_count: number;
}

export interface RunDetail {
  run: Run;
  state: RunState | null;
  state_error: string | null;
}

export interface NodeLogEntry {
  node_name: string;
  thread_id: string;
  timestamp: string;
  input_summary: Record<string, unknown>;
  llm_output: string | null;
  latency_ms: number | null;
  cost_usd: number | null;
}

export type DiscoveryMethod = 'graph_walk' | 'keyword' | 'both' | 'unattributed' | 'unhydrated';

export interface GraphNode {
  channel_id: string;
  title: string | null;
  subscriber_count: number | null;
  discovery_method: DiscoveryMethod;
  max_outlier_score: number;
  video_count: number;
}

export interface GraphEdge {
  source_channel_id: string;
  target_channel_id: string;
  edge_type: string;
  discovered_at: string | null;
}

export interface Channel {
  channel_id: string;
  title: string | null;
  subscriber_count: number | null;
  description: string | null;
  first_seen_at: string | null;
  discovery_method: DiscoveryMethod;
}

export interface OutlierVideo {
  video_id: string;
  channel_id: string;
  channel_title: string | null;
  discovery_method: DiscoveryMethod;
  title: string | null;
  view_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  outlier_score: number | null;
  published_at: string | null;
}

export interface StoreCounts {
  channels: number;
  videos: number;
  discovery_edges: number;
  by_discovery_method: { discovery_method: DiscoveryMethod; channel_count: number }[];
}

export interface Costs {
  total_usd: number;
  by_node: { node_name: string; calls: number; cost_usd: number; avg_latency_ms: number | null }[];
  by_run: { run_id: string; niches: string[]; calls: number; cost_usd: number }[];
}

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* non-JSON error body — keep the status message */
    }
    throw new ApiError(detail, res.status);
  }
  return res.json() as Promise<T>;
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: 'DELETE' });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const parsed = await res.json();
      if (typeof parsed?.detail === 'string') detail = parsed.detail;
    } catch {
      /* non-JSON error body — keep the status message */
    }
    throw new ApiError(detail, res.status);
  }
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const parsed = await res.json();
      if (typeof parsed?.detail === 'string') detail = parsed.detail;
    } catch {
      /* non-JSON error body — keep the status message */
    }
    throw new ApiError(detail, res.status);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () =>
    get<{ status: string; store_reachable: boolean; store_error: string | null }>('/api/health'),

  runs: () => get<Run[]>('/api/runs'),

  run: (runId: string) => get<RunDetail>(`/api/runs/${encodeURIComponent(runId)}`),

  reports: () => get<ReportSummary[]>('/api/reports'),

  logs: (runId: string, since = 0) =>
    get<{ entries: NodeLogEntry[]; cursor: number }>(
      `/api/runs/${encodeURIComponent(runId)}/logs?since=${since}`,
    ),

  counts: () => get<StoreCounts>('/api/store/counts'),

  channels: (q: string, limit = 100) =>
    get<Channel[]>(`/api/store/channels?q=${encodeURIComponent(q)}&limit=${limit}`),

  outliers: (limit = 100) => get<OutlierVideo[]>(`/api/store/videos/outliers?limit=${limit}`),

  graph: (channelId = '', limit = 400) =>
    get<{ nodes: GraphNode[]; edges: GraphEdge[] }>(
      `/api/store/graph?channel_id=${encodeURIComponent(channelId)}&limit=${limit}`,
    ),

  costs: () => get<Costs>('/api/costs'),

  balances: (force = false) =>
    get<Balances>(`/api/balances${force ? '?force=true' : ''}`),

  connectBrightData: (apiKey: string) =>
    post<ProviderBalance>('/api/balances/brightdata/connect', { api_key: apiKey }),

  depths: (topic?: string) =>
    get<DepthTier[]>(
      `/api/depths${topic ? `?topic=${encodeURIComponent(topic)}` : ''}`,
    ),

  thresholds: (depth?: string) =>
    get<Threshold[]>(`/api/thresholds${depth ? `?depth=${encodeURIComponent(depth)}` : ''}`),

  stopRun: (runId: string) =>
    post<{ run_id: string; stopped: boolean; reason?: string }>(
      `/api/runs/${encodeURIComponent(runId)}/stop`, {},
    ),

  deleteRun: (runId: string) =>
    del<{ run_id: string; deleted: boolean }>(`/api/runs/${encodeURIComponent(runId)}`),

  deleteWorkbook: (id: string) =>
    del<{ workbook_id: string; deleted: boolean }>(`/api/workbooks/${encodeURIComponent(id)}`),

  topics: () => get<Topic[]>('/api/topics'),

  suggestSubNiches: (q: string) =>
    get<SubNicheSuggestion>(`/api/topics/suggest?q=${encodeURIComponent(q)}`),

  workbooks: () => get<Workbook[]>('/api/workbooks'),

  workbookSpend: (id: string) => get<WorkbookSpend>(`/api/workbooks/${id}/spend`),

  workbookTree: (id: string) => get<WorkbookTree>(`/api/workbooks/${id}/tree`),

  launchRun: async (
    niches: string[],
    depth?: string,
    thresholds?: Record<string, number>,
  ): Promise<Run> => {
    const body: Record<string, unknown> = { niches };
    if (depth) body.depth = depth;
    if (thresholds && Object.keys(thresholds).length > 0) body.thresholds = thresholds;
    const res = await fetch(`${BASE}/api/runs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = `Could not start the run (${res.status})`;
      try {
        const body = await res.json();
        if (typeof body?.detail === 'string') detail = body.detail;
      } catch {
        /* keep the status message */
      }
      throw new ApiError(detail, res.status);
    }
    return res.json();
  },

  eventsUrl: (runId: string, since: number) =>
    `${BASE}/api/runs/${encodeURIComponent(runId)}/events?since=${since}`,
};
