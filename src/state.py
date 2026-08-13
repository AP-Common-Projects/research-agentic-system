"""HarnessState — the central typed contract for the Omniframes research pipeline.

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

    branch_compactions: Annotated[list[dict], lambda a, b: a + b]

    novelty_rates: Annotated[list[float], lambda a, b: a + b]
    saturated_branches: Annotated[list[str], lambda a, b: a + b]
    budget_spent_usd: Annotated[float, _accumulate_float]

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
        "hydrated_channel_ids": set(),
        "branch_compactions": [],
        "novelty_rates": [],
        "saturated_branches": [],
        "budget_spent_usd": 0.0,
        "next_action": "start",
        "messages": [],
        "errors": [],
        "node_logs": [],
        "schema_version": 3,
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

    state["schema_version"] = version
    return state