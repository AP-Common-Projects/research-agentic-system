"""graph.py — full LangGraph wiring for the v1 harness.

Per master plan §8.2: no dead analyze_deep node. budget_exhausted force-saturates
active nodes then routes to compact_branch. The keyword_search / graph_walk fan-out
has a defined join point (hydrate_metadata) and per-branch failure behavior —
record the error and continue, never silently proceed on partial results without
flagging it in the run's logs (§0.2 item 4).
"""

from __future__ import annotations

import inspect

from langgraph.graph import StateGraph, START, END

from src.state import HarnessState, create_initial_state, migrate_state
from src.observability import write_node_logs
from src.tools.niche_scanner import scan_niches
from src.tools.keyword_search import keyword_search
from src.tools.graph_walk import graph_walk
from src.tools.hydrate_metadata import hydrate_metadata
from src.tools.signal_scoring import score_signals
from src.tools.saturation import check_saturation
from src.tools.graph_clustering import cluster_branch
from src.nodes.taxonomy import build_taxonomy
from src.nodes.compact_branch import compact_branch
from src.nodes.finalize_dataset import finalize_dataset
from src.nodes.select_next_node import select_next_node
from src.nodes.resolve_geo_language import resolve_geo_language
from src.nodes.extract_metadata_signals import extract_metadata_signals
from src.nodes.score_thumbnail_signals import score_thumbnail_signals
from src.nodes.classify_channel import classify_channel
from src.nodes.extract_success_failure_factors import extract_success_failure_factors
from src.nodes.expand_niche_adjacency import expand_niche_adjacency
from src.nodes.niche_queue import has_more_niches, advance_to_next_niche


def _logged(fn, name: str):
    """Flush any node_logs a node produces to the JSONL sink, immediately.

    Wraps both sync and async node functions. Purely observational — never
    changes the node's return value, and sink failures never propagate.
    """
    is_async = inspect.iscoroutinefunction(fn)

    def _sink(state: dict, result: dict) -> dict:
        node_logs = result.get("node_logs") if isinstance(result, dict) else None
        if node_logs:
            write_node_logs(state.get("run_id", ""), node_logs)
        return result

    if is_async:
        async def async_wrapper(state: dict) -> dict:
            result = await fn(state)
            return _sink(state, result)

        async_wrapper.__name__ = name
        return async_wrapper

    def sync_wrapper(state: dict) -> dict:
        result = fn(state)
        return _sink(state, result)

    sync_wrapper.__name__ = name
    return sync_wrapper


def _guarded(fn, name: str):
    """Wrap a fan-out branch so a failure is recorded and flagged, not a silent hang.

    §0.2 item 4: if one branch fails while the other succeeds, the failed branch
    records an error and marks itself done; the run proceeds but the error is
    visible in state["errors"] and node_logs, not swallowed.
    """

    async def wrapper(state: dict) -> dict:
        try:
            return await fn(state)
        except Exception as exc:
            # A delta, not the accumulated list. `errors` is an appending
            # channel: reading it, appending, and returning the whole thing
            # doubles it every round.
            return {
                "errors": [
                    {
                        "node_name": name,
                        "timestamp": "",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "recoverable": False,
                    }
                ],
                f"{name}_done": True,
                "node_logs": [
                    {
                        "node_name": name,
                        "thread_id": state.get("thread_id", ""),
                        "input_summary": {"error": type(exc).__name__},
                    }
                ],
            }

    wrapper.__name__ = name
    return wrapper


def route_after_scan(state: dict) -> list[str]:
    if state.get("selected_niche"):
        return ["build_taxonomy"]
    return [END]


#: Terminal states. Once a run-level ceiling has tripped, the only legal
#: destination is finalize_dataset — see route_after_select.
_TERMINAL_ACTIONS = {"all_done", "budget_exhausted"}


def route_after_select(state: dict) -> list[str]:
    """Dispatch the discovery fan-out, unless the run is finished or broke.

    `budget_exhausted` has to be terminal here, not just at check_saturation.
    The circuit breaker fires *downstream* of the spend: check_saturation trips
    a ceiling, routes to compact_branch, compaction proposes a new node,
    route_after_compaction sends that to select_next_node, and select_next_node
    activates it without clearing next_action. Without this guard the run then
    dispatches a full paid discovery round after the ceiling already tripped —
    once per proposable node, and under the `full` profile (every cap 0) that
    is bounded only by the recursion limit.
    """
    if state.get("next_action") in _TERMINAL_ACTIONS:
        return ["finalize_dataset"]
    # v4: next_niche routing for cluster-wide traversal
    if state.get("next_action") == "next_niche":
        return ["build_taxonomy"]
    return ["keyword_search", "graph_walk"]


def route_after_saturation(state: dict) -> list[str]:
    action = state.get("next_action", "expand_deeper")
    if action == "expand_deeper":
        return ["select_next_node"]
    if action == "budget_exhausted":
        return ["compact_branch"]
    return ["cluster_branch"]


def route_after_compaction(state: dict) -> list[str]:
    if state.get("next_action") == "budget_exhausted":
        return ["finalize_dataset"]
    tree = state.get("tree", {})
    has_pending = any(n.get("status") == "pending" for n in tree.values())
    has_proposed = any(n.get("proposed_new_nodes") for n in tree.values())
    if has_pending or has_proposed:
        return ["select_next_node"]
    # v4: check for more niches before finalization
    if has_more_niches(state):
        return ["select_next_node"]  # select_next_node will emit next_niche
    return ["extract_success_failure_factors"]


def route_after_floor(state: dict) -> list[str]:
    """v3 floor gate: only qualifying channels get expensive LLM/vision."""
    if state.get("floor_gate_eligible"):
        return ["score_thumbnail_signals"]
    return ["check_saturation"]


def build_graph() -> StateGraph:
    graph = StateGraph(HarnessState)

    graph.add_node("scan_niches", _logged(scan_niches, "scan_niches"))
    graph.add_node("expand_niche_adjacency", _logged(expand_niche_adjacency, "expand_niche_adjacency"))
    graph.add_node("build_taxonomy", _logged(build_taxonomy, "build_taxonomy"))
    graph.add_node("select_next_node", _logged(select_next_node, "select_next_node"))
    graph.add_node("keyword_search", _logged(_guarded(keyword_search, "keyword_search"), "keyword_search"))
    graph.add_node("graph_walk", _logged(_guarded(graph_walk, "graph_walk"), "graph_walk"))
    graph.add_node("hydrate_metadata", _logged(hydrate_metadata, "hydrate_metadata"))
    graph.add_node("resolve_geo_language", _logged(resolve_geo_language, "resolve_geo_language"))
    graph.add_node("extract_metadata_signals", _logged(extract_metadata_signals, "extract_metadata_signals"))
    graph.add_node("score_signals", _logged(score_signals, "score_signals"))
    graph.add_node("score_thumbnail_signals", _logged(score_thumbnail_signals, "score_thumbnail_signals"))
    graph.add_node("classify_channel", _logged(classify_channel, "classify_channel"))
    graph.add_node("check_saturation", _logged(check_saturation, "check_saturation"))
    graph.add_node("cluster_branch", _logged(cluster_branch, "cluster_branch"))
    graph.add_node("compact_branch", _logged(compact_branch, "compact_branch"))
    graph.add_node("extract_success_failure_factors", _logged(extract_success_failure_factors, "extract_success_failure_factors"))
    graph.add_node("finalize_dataset", _logged(finalize_dataset, "finalize_dataset"))

    graph.add_edge(START, "scan_niches")
    graph.add_edge("scan_niches", "expand_niche_adjacency")
    graph.add_edge("expand_niche_adjacency", "build_taxonomy")
    graph.add_conditional_edges(
        "select_next_node",
        route_after_select,
        ["keyword_search", "graph_walk", "finalize_dataset"],
    )
    graph.add_edge("keyword_search", "hydrate_metadata")
    graph.add_edge("graph_walk", "hydrate_metadata")
    graph.add_edge("hydrate_metadata", "resolve_geo_language")
    graph.add_edge("resolve_geo_language", "extract_metadata_signals")
    graph.add_edge("extract_metadata_signals", "score_signals")
    # v3 floor gate: expensive LLM/vision only for qualifying channels
    graph.add_conditional_edges(
        "score_signals", route_after_floor,
        ["score_thumbnail_signals", "check_saturation"],
    )
    graph.add_edge("score_thumbnail_signals", "classify_channel")
    graph.add_edge("classify_channel", "check_saturation")
    graph.add_conditional_edges(
        "check_saturation",
        route_after_saturation,
        ["select_next_node", "cluster_branch", "compact_branch"],
    )
    graph.add_edge("cluster_branch", "compact_branch")
    graph.add_conditional_edges(
        "compact_branch",
        route_after_compaction,
        ["select_next_node", "extract_success_failure_factors", "finalize_dataset"],
    )
    graph.add_edge("extract_success_failure_factors", "finalize_dataset")
    graph.add_edge("finalize_dataset", END)

    return graph


def compile_graph(checkpointer=None):
    graph = build_graph()
    if checkpointer is not None:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()


async def run_pipeline(
    candidate_niches: list[str],
    run_id: str,
    thread_id: str,
    checkpointer=None,
    resume: bool = False,
    run_mode: str = "cold_start",
    state_overrides: dict | None = None,
) -> dict:
    """Run the full pipeline end-to-end. Returns the final state dict.

    With resume=True and a checkpointer, loads the checkpointed state for
    thread_id (migrating it first) and continues from where it left off,
    instead of overwriting checkpointed state with a fresh initial state.
    """
    # LangGraph's default recursion_limit is 25 supersteps, which a legitimate
    # multi-branch run exceeds — and when it trips it raises rather than
    # producing a report. The real termination guarantees are the governors in
    # check_saturation; this is the backstop behind them, set explicitly so the
    # ceiling is a decision rather than an inherited default.
    from src.config import get_config

    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": get_config().harness.graph_recursion_limit,
    }
    app = compile_graph(checkpointer=checkpointer)

    if resume and checkpointer is not None:
        # Async accessors, to match the async checkpointer the pipeline runs
        # on — the sync `get_state`/`update_state` reach for `get_tuple`/`put`,
        # which AsyncPostgresSaver does not implement.
        snapshot = await app.aget_state(config)
        if snapshot.values:
            existing = dict(snapshot.values)
            migrated = migrate_state(dict(existing))

            # Write back ONLY the keys migration added. update_state applies
            # values through the channel reducers, exactly as if a node had
            # returned them — so handing it the full snapshot re-adds every
            # accumulator to itself: budget_spent_usd and
            # brightdata_records_used double, errors and node_logs duplicate.
            # That is fatal now that those fields are the cost governors: a
            # resumed run reads its spend at 2x and check_saturation trips
            # budget_exhausted before doing any work, compounding 4x, 8x on
            # each further resume. New keys are safe because their channels
            # are empty, so reducer(empty, default) == default.
            # Key-presence alone is the wrong test: LangGraph materialises
            # every declared channel into snapshot.values, so a v4 checkpoint
            # arrives with all the v5 keys already present but empty. Filtering
            # on `k not in existing` would therefore drop migration's real
            # work — notably the expanded_channel_refs backfill, whose whole
            # job is to stop a resumed run re-walking and re-billing every
            # channel it already paid for.
            #
            # So: include a key when it is new, or when migration filled a
            # value that was empty. Never when the existing value is already
            # populated — that is the case that would double an accumulator.
            delta = {
                k: v
                for k, v in migrated.items()
                if k not in existing or (not existing.get(k) and v)
            }
            if migrated.get("schema_version") != existing.get("schema_version"):
                delta["schema_version"] = migrated["schema_version"]
            if delta:
                await app.aupdate_state(config, delta)

            final = await app.ainvoke(None, config=config)
            return migrate_state(final)

    initial = create_initial_state(run_id, thread_id, candidate_niches)
    if run_mode == "augment":
        from src.state import create_augmented_state
        initial = create_augmented_state(run_id, thread_id, candidate_niches)
    initial.setdefault("run_mode", run_mode)
    if state_overrides:
        initial.update(state_overrides)

    # v3: initialize harness_runs row at run start
    try:
        from src.db.connection import get_connection, put_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO harness_runs (run_id, thread_id, seed_niches, config_snapshot) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
                (run_id, thread_id, candidate_niches, json.dumps({"profile": get_config().harness.profile})),
            )
            conn.commit()
            cur.close()
        finally:
            put_connection(conn)
    except Exception:
        pass

    import json
    final = await app.ainvoke(initial, config=config)
    return migrate_state(final)
