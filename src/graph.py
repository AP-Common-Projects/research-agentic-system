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
from src.nodes.taxonomy import build_taxonomy
from src.nodes.compact_branch import compact_branch
from src.nodes.synthesize import synthesize
from src.nodes.select_next_node import select_next_node


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
            errors = list(state.get("errors", []))
            errors.append(
                {
                    "node_name": name,
                    "timestamp": "",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "recoverable": False,
                }
            )
            return {
                "errors": errors,
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


def route_after_select(state: dict) -> list[str]:
    if state.get("next_action") == "all_done":
        return ["synthesize"]
    return ["keyword_search", "graph_walk"]


def route_after_saturation(state: dict) -> list[str]:
    action = state.get("next_action", "expand_deeper")
    if action == "expand_deeper":
        return ["select_next_node"]
    return ["compact_branch"]


def route_after_compaction(state: dict) -> list[str]:
    tree = state.get("tree", {})
    has_pending = any(n.get("status") == "pending" for n in tree.values())
    has_proposed = any(n.get("proposed_new_nodes") for n in tree.values())
    if has_pending or has_proposed:
        return ["select_next_node"]
    return ["synthesize"]


def build_graph() -> StateGraph:
    graph = StateGraph(HarnessState)

    graph.add_node("scan_niches", _logged(scan_niches, "scan_niches"))
    graph.add_node("build_taxonomy", _logged(build_taxonomy, "build_taxonomy"))
    graph.add_node("select_next_node", _logged(select_next_node, "select_next_node"))
    graph.add_node("keyword_search", _logged(_guarded(keyword_search, "keyword_search"), "keyword_search"))
    graph.add_node("graph_walk", _logged(_guarded(graph_walk, "graph_walk"), "graph_walk"))
    graph.add_node("hydrate_metadata", _logged(hydrate_metadata, "hydrate_metadata"))
    graph.add_node("score_signals", _logged(score_signals, "score_signals"))
    graph.add_node("check_saturation", _logged(check_saturation, "check_saturation"))
    graph.add_node("compact_branch", _logged(compact_branch, "compact_branch"))
    graph.add_node("synthesize", _logged(synthesize, "synthesize"))

    graph.add_edge(START, "scan_niches")
    graph.add_conditional_edges(
        "scan_niches", route_after_scan, ["build_taxonomy", END]
    )
    graph.add_edge("build_taxonomy", "select_next_node")
    graph.add_conditional_edges(
        "select_next_node",
        route_after_select,
        ["keyword_search", "graph_walk", "synthesize"],
    )
    graph.add_edge("keyword_search", "hydrate_metadata")
    graph.add_edge("graph_walk", "hydrate_metadata")
    graph.add_edge("hydrate_metadata", "score_signals")
    graph.add_edge("score_signals", "check_saturation")
    graph.add_conditional_edges(
        "check_saturation",
        route_after_saturation,
        ["select_next_node", "compact_branch"],
    )
    graph.add_conditional_edges(
        "compact_branch",
        route_after_compaction,
        ["select_next_node", "synthesize"],
    )
    graph.add_edge("synthesize", END)

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
    state_overrides: dict | None = None,
) -> dict:
    """Run the full pipeline end-to-end. Returns the final state dict.

    With resume=True and a checkpointer, loads the checkpointed state for
    thread_id (migrating it first) and continues from where it left off,
    instead of overwriting checkpointed state with a fresh initial state.
    """
    config = {"configurable": {"thread_id": thread_id}}
    app = compile_graph(checkpointer=checkpointer)

    if resume and checkpointer is not None:
        snapshot = app.get_state(config)
        if snapshot.values:
            migrated = migrate_state(dict(snapshot.values))
            app.update_state(config, migrated)
            final = await app.ainvoke(None, config=config)
            return migrate_state(final)

    initial = create_initial_state(run_id, thread_id, candidate_niches)
    if state_overrides:
        initial.update(state_overrides)

    final = await app.ainvoke(initial, config=config)
    return migrate_state(final)
