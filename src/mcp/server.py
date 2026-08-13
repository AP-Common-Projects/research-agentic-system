"""MCP server wrapper exposing run_niche_scan / run_deep_research / query_store."""

from __future__ import annotations

import asyncio
import json
import uuid

from src.graph import run_pipeline
from src.db.connection import close_pools
from src.db.schema import ensure_schema
from src.db.checkpointer import get_checkpointer
from src.nodes.store import get_store


def _run_niche_scan(candidate_niches: list[str]) -> dict:
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    thread_id = f"thread-{uuid.uuid4().hex[:12]}"
    ensure_schema()
    checkpointer = get_checkpointer()
    final = asyncio.run(
        run_pipeline(
            candidate_niches=candidate_niches,
            run_id=run_id,
            thread_id=thread_id,
            checkpointer=checkpointer,
        )
    )
    close_pools()
    return final


def run_niche_scan(candidate_niches: list[str]) -> dict:
    """Scan a list of candidate niches and return the graded report + ranked evidence."""
    final = _run_niche_scan(candidate_niches)
    return {
        "selected_niche": final.get("selected_niche", ""),
        "niche_scanner_evidence": final.get("niche_scanner_evidence", {}),
        "final_report": final.get("final_report"),
    }


def run_deep_research(niche: str) -> dict:
    """Run deep research on a single niche (bypasses the scanner)."""
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    thread_id = f"thread-{uuid.uuid4().hex[:12]}"
    ensure_schema()
    checkpointer = get_checkpointer()
    final = asyncio.run(
        run_pipeline(
            candidate_niches=[niche],
            run_id=run_id,
            thread_id=thread_id,
            checkpointer=checkpointer,
        )
    )
    close_pools()
    return {"final_report": final.get("final_report")}


async def _query_store(question: str) -> dict:
    store = get_store()
    videos = await store.get_all_videos()
    channels = await store.get_channels_by_ids(
        list({v.get("channel_id", "") for v in videos if v.get("channel_id")})
    )
    return {
        "question": question,
        "channels": channels[:100],
        "videos": videos[:100],
        "counts": {"channels": len(channels), "videos": len(videos)},
    }


def query_store(question: str) -> dict:
    """Query the structured store for hydrated channels and videos."""
    return asyncio.run(_query_store(question))


async def _mcp_main() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        return

    mcp = FastMCP("niche-research-harness")

    @mcp.tool()
    def run_niche_scan_tool(candidate_niches: list[str]) -> dict:
        return run_niche_scan(candidate_niches)

    @mcp.tool()
    def run_deep_research_tool(niche: str) -> dict:
        return run_deep_research(niche)

    @mcp.tool()
    def query_store_tool(question: str) -> dict:
        return query_store(question)

    await mcp.run_stdio_async()


def main() -> None:
    asyncio.run(_mcp_main())


if __name__ == "__main__":
    main()
