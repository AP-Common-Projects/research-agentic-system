"""breakout_scanner — video-first breakout discovery (v4, plan §10).

Uses Bright Data's keyword channel discovery to sample recent videos in a
niche's keyword space and flag channels with views far above what their
subscriber count would predict. Tags discovery_method='breakout_video_discovery'.
"""

from __future__ import annotations

import time

from src.config import get_config
from src.state import NodeLog, ErrorRecord
from src.tools.bright_data import BrightDataClient


def compute_breakout_signal(video_views: int, channel_subs: int) -> bool:
    if channel_subs <= 0:
        return video_views >= 100000
    return video_views >= channel_subs * 5


async def breakout_scanner(state: dict) -> dict:
    thread_id = state.get("thread_id", "")
    start = time.monotonic()
    cfg = get_config().harness
    errors: list[dict] = []

    def _log(input_summary: dict) -> list[dict]:
        return [NodeLog(
            node_name="breakout_scanner",
            thread_id=thread_id,
            input_summary=input_summary,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=0.0,
        ).model_dump()]

    tree = state.get("tree", {})
    active_node_id = state.get("active_node_id")
    if not active_node_id or active_node_id not in tree:
        return {"node_logs": _log({"reason": "no active node", "scanned": 0})}

    node = tree[active_node_id]
    keywords = node.get("keywords", [])
    if not keywords:
        return {"node_logs": _log({"reason": "no keywords", "scanned": 0})}

    client = BrightDataClient()
    breakout_channels: set[str] = set()
    records = 0

    for kw in keywords[:3]:
        try:
            results, used = await client.discover_channels_by_keyword(
                [kw], limit_per_input=5
            )
            records += used
            for video in results:
                ch_id = video.get("channel_id", "")
                v_views = int(video.get("view_count") or 0)
                ch_subs = int(video.get("subscriber_count") or 0)
                if ch_id and compute_breakout_signal(v_views, ch_subs):
                    breakout_channels.add(ch_id)
        except Exception as exc:
            errors.append(ErrorRecord(
                node_name="breakout_scanner",
                error_type=type(exc).__name__,
                message=f"breakout scan failed for '{kw}': {exc}",
                recoverable=True,
            ).model_dump())

    discovered_set = set(state.get("discovered_channel_ids", []))
    truly_new = [c for c in breakout_channels if c not in discovered_set]
    cost = round(records * cfg.brightdata_cost_per_record_usd, 8)

    return {
        "discovered_channel_ids": truly_new,
        "keyword_channel_ids": breakout_channels,
        "brightdata_records_used": records,
        "budget_spent_usd": cost,
        "node_logs": _log({
            "keywords_sampled": len(keywords[:3]),
            "results": records,
            "breakout_channels_found": len(breakout_channels),
            "new_discoveries": len(truly_new),
        }),
        "errors": errors,
    }