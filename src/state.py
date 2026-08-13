"""HarnessState — the central typed contract for the niche-research pipeline.

Every field audited per §0.2 item 1 of the master plan:
"If two nodes write this in the same step, what should happen?"

Reducers are explicit, not assumed. Default overwrite is correct for single-writer
fields; accumulating fields use Annotated with the right merge strategy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Optional, TypedDict, Union

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------

def _merge_discovered_channels(existing: list[str], new: list[str]) -> list[str]:
    """Dedup-aware append — same channel found by both parallel branches appears once."""
    seen = set(existing)
    return existing + [ch for ch in new if ch not in seen and not seen.add(ch)]


def _merge_discovered_videos(existing: list[str], new: list[str]) -> list[str]:
    """Dedup-aware append for video IDs."""
    seen = set(existing)
    return existing + [vid for vid in new if vid not in seen and not seen.add(vid)]


def _merge_set_union(existing: set[str], new: set[str]) -> set[str]:
    """Union reducer for visited/expanded ID sets."""
    return existing | new


def _merge_unique_append(existing: list[str], new: list[str]) -> list[str]:
    """Append-with-dedup, ordered.

    A plain `a + b` reducer is only safe if every writer returns a *delta*. A
    node that reads the accumulated list, appends to it, and returns the whole
    thing doubles the list on every round — [X] + [X] = [X, X], then 4, 8, 16.
    That is a real bug this project hit: it OOM-killed the end-to-end test once
    the graph recursion limit was raised enough for the doubling to bite.

    Writers should still return deltas, but making the reducer idempotent means
    the failure mode is a no-op instead of exponential growth.
    """
    seen = set(existing)
    return existing + [item for item in new if item not in seen and not seen.add(item)]


def _merge_tree_dict(
    existing: dict[str, TreeNode],
    new: dict[str, TreeNode],
) -> dict[str, TreeNode]:
    """Deep per-field merge for tree nodes.

    keyword_search and graph_walk both write to the SAME node_id in the same
    fan-out step, but update disjoint fields (keyword: queries_run / novelty;
    graph walk: unexpanded_channel_ids / novelty). A last-write-wins merge
    silently drops one branch's fields — this is the missing-reducer bug class.
    Per-field merge keeps both, with the later write winning only on overlap.
    """
    merged = dict(existing)
    for node_id, new_node in new.items():
        if node_id in merged:
            combined = dict(merged[node_id])
            combined.update(new_node)
            merged[node_id] = combined
        else:
            merged[node_id] = dict(new_node)
    return merged


def _accumulate_float(a: float, b: float) -> float:
    return a + b


def _accumulate_int(a: int, b: int) -> int:
    """Accumulating counter for consumed external resources.

    These have to live in state rather than on the API client, because
    hydrate_metadata and graph_walk construct a *fresh* client on every round —
    an instance counter therefore resets to zero each round and a per-run
    ceiling built on it can never fire.
    """
    return (a or 0) + (b or 0)


def _merge_round_counts(existing: dict[str, int], new: dict[str, int]) -> dict[str, int]:
    """Per-node round counter, max-wins.

    Both discovery tracks report the round they just finished for the same
    active node in the same superstep. Summing would double-count and halve the
    effective round cap; max is the honest merge.
    """
    merged = dict(existing or {})
    for node_id, count in (new or {}).items():
        merged[node_id] = max(merged.get(node_id, 0), count)
    return merged


# ---------------------------------------------------------------------------
# Pydantic models for structured sub-state
# ---------------------------------------------------------------------------

class ProposedNode(BaseModel):
    """A new tree node proposed by compact_branch when it finds an off-taxonomy cluster."""
    label: str
    rationale: str
    seed_channel_ids: list[str] = Field(default_factory=list)
    parent_id: str


class TreeNode(BaseModel):
    """A single node in the taxonomy tree."""
    id: str
    label: str
    depth: int = 0
    parent_id: Optional[str] = None
    children_ids: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    seed_channel_ids: list[str] = Field(default_factory=list)
    unexpanded_channel_ids: list[str] = Field(default_factory=list)
    status: Literal["pending", "active", "saturated", "compacted"] = "pending"
    compaction_summary: Optional[str] = None
    proposed_new_nodes: list[ProposedNode] = Field(default_factory=list)
    schema_version: int = 1
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    saturated_at: Optional[str] = None


class BranchCompaction(BaseModel):
    """Result of compact_branch processing a single leaf node."""
    node_id: str
    node_label: str
    channel_count: int
    video_count: int
    top_channels: list[dict[str, Any]] = Field(default_factory=list)
    narrative_summary: str = ""
    key_patterns: list[dict[str, Any]] = Field(default_factory=list)
    proposed_nodes: list[ProposedNode] = Field(default_factory=list)
    compiled_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class GradedFinding(BaseModel):
    """A single finding from synthesis with evidence-grade annotation."""
    claim: str
    grade: Literal["strong", "moderate", "weak"]
    evidence: dict[str, Any] = Field(default_factory=dict)
    supporting_channel_ids: list[str] = Field(default_factory=list)
    pattern_type: str = ""


class FinalReport(BaseModel):
    """The final graded research report."""
    niche: str
    run_id: str
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    summary: str = ""
    findings: list[GradedFinding] = Field(default_factory=list)
    cannot_determine: list[str] = Field(default_factory=list)
    discovery_stats: dict[str, Any] = Field(default_factory=dict)


class NodeLog(BaseModel):
    """Per-node structured log entry."""
    node_name: str
    thread_id: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    input_summary: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    llm_output: Optional[str] = None
    latency_ms: Optional[float] = None
    cost_usd: Optional[float] = None


class ErrorRecord(BaseModel):
    """Structured error for accumulation in state."""
    node_name: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error_type: str
    message: str
    recoverable: bool = True


class NicheScannerEvidence(BaseModel):
    """Evidence collected by scan_niches for a candidate niche."""
    niche_name: str
    opportunity_score: float = 0.0
    channel_count_estimate: int = 0
    avg_views: float = 0.0
    avg_subs: float = 0.0
    growth_indicators: list[str] = Field(default_factory=list)
    saturation_signal: str = "unknown"


# ---------------------------------------------------------------------------
# HarnessState — the graph's TypedDict (LangGraph state schema)
# ---------------------------------------------------------------------------

class HarnessState(TypedDict, total=False):
    run_id: str
    thread_id: str

    candidate_niches: Annotated[list[str], lambda a, b: b]
    selected_niche: str
    niche_scanner_evidence: dict

    tree: Annotated[dict[str, dict], _merge_tree_dict]
    active_node_id: Optional[str]

    discovered_channel_ids: Annotated[list[str], _merge_discovered_channels]
    discovered_video_ids: Annotated[list[str], _merge_discovered_videos]
    visited_channel_ids: Annotated[set[str], _merge_set_union]
    expanded_channel_ids: Annotated[set[str], _merge_set_union]
    hydrated_channel_ids: Annotated[set[str], _merge_set_union]

    # Graph traversal is keyed on channel *refs* (handle/URL), not UC ids:
    # featured_channels edges and taxonomy seeds both arrive as handles, and
    # the UC id only exists once the channel record has been fetched. This is
    # the real "already expanded" set the frontier subtracts.
    expanded_channel_refs: Annotated[set[str], _merge_set_union]

    # Per-track discovery attribution. Kept as two independent sets rather than
    # one label per channel because a channel found by BOTH tracks must stay
    # recorded in both — collapsing to a single label loses the very comparison
    # the project exists to make (master plan §1: the graph-walk track must
    # demonstrably surface channels the keyword track missed). Union reducers,
    # so the parallel fan-out writes cannot clobber each other.
    keyword_channel_ids: Annotated[set[str], _merge_set_union]
    graph_walk_channel_ids: Annotated[set[str], _merge_set_union]

    branch_compactions: Annotated[list[dict], lambda a, b: a + b]

    novelty_rates: Annotated[list[float], lambda a, b: a + b]
    saturated_branches: Annotated[list[str], _merge_unique_append]
    budget_spent_usd: Annotated[float, _accumulate_float]

    # Non-LLM spend. Until these existed, budget_spent_usd only ever saw LLM
    # tokens — which are the *small* cost here — so the circuit breaker was
    # blind to Bright Data records and YouTube quota, the two resources that
    # can actually be exhausted.
    brightdata_records_used: Annotated[int, _accumulate_int]
    youtube_quota_used: Annotated[int, _accumulate_int]

    # Round counter per tree node, so a branch that never saturates still
    # terminates. See check_saturation's max_rounds_per_branch governor.
    rounds_by_node: Annotated[dict[str, int], _merge_round_counts]

    next_action: str

    messages: Annotated[list, add_messages]

    errors: Annotated[list[dict], lambda a, b: a + b]

    node_logs: Annotated[list[dict], lambda a, b: a + b]

    schema_version: int

    final_report: Optional[dict]

    keyword_search_done: bool
    graph_walk_done: bool


def create_initial_state(
    run_id: str,
    thread_id: str,
    candidate_niches: list[str],
    budget_limit_usd: float = 10.0,
) -> dict:
    """Factory for a clean starting state."""
    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "candidate_niches": candidate_niches,
        "selected_niche": "",
        "niche_scanner_evidence": {},
        "tree": {},
        "active_node_id": None,
        "discovered_channel_ids": [],
        "discovered_video_ids": [],
        "visited_channel_ids": set(),
        "expanded_channel_ids": set(),
        "expanded_channel_refs": set(),
        "hydrated_channel_ids": set(),
        "keyword_channel_ids": set(),
        "graph_walk_channel_ids": set(),
        "branch_compactions": [],
        "novelty_rates": [],
        "saturated_branches": [],
        "budget_spent_usd": 0.0,
        "brightdata_records_used": 0,
        "youtube_quota_used": 0,
        "rounds_by_node": {},
        "next_action": "start",
        "messages": [],
        "errors": [],
        "node_logs": [],
        "schema_version": 5,
        "final_report": None,
        "keyword_search_done": False,
        "graph_walk_done": False,
    }


def migrate_state(state: dict) -> dict:
    """Run on load to migrate checkpoints from older schema versions.

    Per §0.2 item 6 of the master plan: any schema or taxonomy change
    at runtime needs a version marker + migrate function, or checkpoints
    created before the change fail to resume.
    """
    version = state.get("schema_version", 0)

    if version < 1:
        state.setdefault("run_id", "")
        state.setdefault("thread_id", "")
        state.setdefault("errors", [])
        state.setdefault("node_logs", [])
        state.setdefault("budget_spent_usd", 0.0)
        state.setdefault("final_report", None)
        state.setdefault("keyword_search_done", False)
        state.setdefault("graph_walk_done", False)
        version = 1

    if version < 2:
        state.setdefault("saturated_branches", [])
        state.setdefault("niche_scanner_evidence", {})
        version = 2

    if version < 3:
        state.setdefault("hydrated_channel_ids", set())
        version = 3

    if version < 4:
        # Pre-v4 checkpoints have no per-track attribution. Backfilling is not
        # possible — the store recorded a single flat label — so these stay
        # empty and the API reports such channels as "unattributed" rather
        # than guessing which track found them.
        state.setdefault("keyword_channel_ids", set())
        state.setdefault("graph_walk_channel_ids", set())
        version = 4

    if version < 5:
        # Cost governors. Counters start at zero rather than being backfilled:
        # a resumed pre-v5 run has already spent records we have no record of,
        # so the honest position is that this run's budget starts now. The
        # ceiling still binds going forward, which is what it is for.
        state.setdefault("brightdata_records_used", 0)
        state.setdefault("youtube_quota_used", 0)
        state.setdefault("rounds_by_node", {})
        # Ref-keyed traversal replaced id-keyed traversal. Pre-v5 checkpoints
        # recorded expanded UC ids, which normalize cleanly to channel URLs —
        # so this one CAN be migrated rather than dropped, and a resumed run
        # will not re-expand and re-bill channels it already walked.
        #
        # Truthiness, not key presence: LangGraph materialises EVERY declared
        # channel into snapshot.values, so a v4 checkpoint still arrives with
        # `expanded_channel_refs` present and equal to set(). A `not in` guard
        # therefore never fires and the backfill silently does nothing, which
        # is exactly the re-billing this block exists to prevent.
        if not state.get("expanded_channel_refs"):
            state["expanded_channel_refs"] = {
                f"https://www.youtube.com/channel/{cid}"
                for cid in state.get("expanded_channel_ids", set())
                if cid
            }
        version = 5

    state["schema_version"] = version
    return state