"""Minimal CLI wrapper over the internal API for headless/scripted runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from src.graph import run_pipeline
from src.db.connection import close_async_pool, close_pools
from src.db.schema import ensure_schema
from src.observability.logging_config import configure_logging
from src.db.checkpointer import get_async_checkpointer
from scripts.quota_budget_check import check_resource, format_report


def _preflight_quota_check(config_path: str) -> bool:
    """Run the quota_budget_check gate before starting a run.

    Returns True if the run should proceed, False if a resource EXCEEDS its
    ceiling and the run should be aborted before burning any real quota.
    """
    with open(config_path) as f:
        config = json.load(f)

    results = [check_resource(r) for r in config["resources"]]
    print(format_report(results))

    if any(r["status"] == "EXCEEDS" for r in results):
        print("\nAborting: at least one resource EXCEEDS its ceiling per the quota config.")
        return False
    if any(r["status"] == "WARN" for r in results):
        print("\nWARNING: at least one resource is at or above its warn threshold. Proceeding.")
    return True


async def _run(niches: list[str], resume_thread_id: str | None) -> dict:
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    thread_id = resume_thread_id or f"thread-{uuid.uuid4().hex[:12]}"

    ensure_schema()
    checkpointer = await get_async_checkpointer()

    final = await run_pipeline(
        candidate_niches=niches,
        run_id=run_id,
        thread_id=thread_id,
        checkpointer=checkpointer,
        resume=resume_thread_id is not None,
    )
    # Export under the run_id the DATA was actually tagged with, not the one
    # generated above. On a fresh run those are the same id — create_initial_state
    # stamps state["run_id"] with exactly this value. On --resume they are not:
    # this function mints a brand-new run_id on every invocation, but the
    # checkpointed state (and everything hydrate_metadata tagged in
    # category_tags under it) keeps the ORIGINAL run_id — the resume-delta
    # logic in run_pipeline deliberately never overwrites an existing key.
    # Exporting under the freshly-generated id queried category_tags for a run
    # that tagged nothing, and produced a report with real prose sitting above
    # "channels: 0, videos: 0, edges: 0" — the underlying data was never
    # missing, just addressed by the wrong key.
    export_run_id = final.get("run_id") or run_id
    # Export before tearing down the pools. A run that completes and is
    # never exported leaves its deliverable only in a checkpoint, and the
    # store gets cleared between runs.
    try:
        from src.export import export_run

        path = export_run(export_run_id, thread_id)
        print(f"\nExported to {path}")
    except Exception as exc:
        print(f"\nExport failed ({type(exc).__name__}: {exc}) — the run itself is unaffected.")

    await close_async_pool()
    close_pools()
    return final


def _already_spent_by_thread(thread_id: str) -> int:
    """This thread's own brightdata_records_used, read from its checkpoint.

    Synchronous and cheap — a plain get_state, not a graph run. Used only to
    make the account-budget preflight resume-aware; failures here must not
    block a resume, so an unreadable checkpoint falls back to 0 (the
    conservative-for-a-fresh-run behaviour, at worst re-adding the full
    per-run budget the way a first attempt would).
    """
    try:
        from src.api.server import _load_checkpoint

        state = _load_checkpoint(thread_id)
        return int((state or {}).get("brightdata_records_used", 0) or 0)
    except Exception:
        return 0


def _preflight_account_budget(resume_thread_id: str | None = None) -> bool:
    """Refuse to start if this run could breach the account-level ceiling.

    Checked before the run, not during: the per-run governors cannot see the
    wallet, so without this the Nth run happily spends the same budget the
    first one did. Returns True if the run may proceed.

    Resume-aware: a resumed thread has already spent some of its own
    brightdata_record_budget, and that spend is already inside
    total_records_spent()'s ledger total. Projecting the FULL per-run budget
    on top of the ledger — as a fresh run correctly does — double-counts a
    resume's own past spend and can abort a resume that cannot actually
    breach the ceiling. Observed live: Legal's resume aborted projecting
    6510 + 4000 = 10510 when it had already spent 1513 of that 4000 and its
    true remaining exposure was 2487.
    """
    from src.config import get_config
    from src.observability.logging_config import total_records_spent

    cfg = get_config().harness
    ceiling = cfg.brightdata_account_record_budget
    if ceiling <= 0:
        return True

    spent = total_records_spent()
    already_this_run = (
        _already_spent_by_thread(resume_thread_id) if resume_thread_id else 0
    )
    remaining_this_run = max(0, cfg.brightdata_record_budget - already_this_run)
    projected = spent + remaining_this_run

    note = (
        f" ({already_this_run} of that already spent by this thread)"
        if already_this_run
        else ""
    )
    print(
        f"Account budget: {spent} records already spent across all runs "
        f"(${spent * cfg.brightdata_cost_per_record_usd:.2f}); "
        f"this run may add up to {remaining_this_run}{note}, "
        f"against a ceiling of {ceiling}."
    )
    if projected > ceiling:
        print(
            f"\nAborting: this run could reach {projected} records, over the "
            f"{ceiling} account ceiling. Raise BRIGHTDATA_ACCOUNT_RECORD_BUDGET "
            f"or lower BRIGHTDATA_RECORD_BUDGET."
        )
        return False
    return True


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="YouTube niche research harness")
    parser.add_argument("niches", nargs="+", help="Candidate niche topic strings (10-30 recommended)")
    parser.add_argument("--resume", metavar="THREAD_ID", help="Resume a prior run by thread_id")
    parser.add_argument("--json", action="store_true", help="Emit final state as JSON")
    parser.add_argument(
        "--quota-check",
        metavar="CONFIG_JSON",
        help=(
            "Path to a quota_budget_check config (see "
            "scripts/quota_budget_check.py --write-example). Run as a "
            "pre-flight gate; aborts before starting if any resource EXCEEDS."
        ),
    )
    args = parser.parse_args()

    # Parsed before the account-budget check so a resume can be recognised —
    # the check needs --resume to look up what this thread has already spent.
    if not _preflight_account_budget(args.resume):
        raise SystemExit(1)

    if args.quota_check and not _preflight_quota_check(args.quota_check):
        raise SystemExit(1)

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
