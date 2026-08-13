"""Minimal CLI wrapper over the internal API for headless/scripted runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from src.graph import run_pipeline
from src.db.connection import close_pools
from src.db.schema import ensure_schema
from src.db.checkpointer import get_checkpointer


async def _run(niches: list[str], resume_thread_id: str | None) -> dict:
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    thread_id = resume_thread_id or f"thread-{uuid.uuid4().hex[:12]}"

    ensure_schema()
    checkpointer = get_checkpointer()

    final = await run_pipeline(
        candidate_niches=niches,
        run_id=run_id,
        thread_id=thread_id,
        checkpointer=checkpointer,
        resume=resume_thread_id is not None,
    )
    close_pools()
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Omniframes YouTube niche research harness")
    parser.add_argument("niches", nargs="+", help="Candidate niche topic strings (10-30 recommended)")
    parser.add_argument("--resume", metavar="THREAD_ID", help="Resume a prior run by thread_id")
    parser.add_argument("--json", action="store_true", help="Emit final state as JSON")
    args = parser.parse_args()

    final = asyncio.run(_run(args.niches, args.resume))

    report = final.get("final_report")
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        if report:
            print(f"Niche: {report.get('niche')}")
            print(f"Summary: {report.get('summary', '')[:500]}")
            for f in report.get("findings", []):
                print(f"  [{f.get('grade')}] {f.get('claim')}")
        else:
            print("No final report produced.")
            for e in final.get("errors", []):
                print(f"  ERROR: {e.get('error_type')}: {e.get('message')}")


if __name__ == "__main__":
    main()
