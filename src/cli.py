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


async def _run(
    niches: list[str],
    resume_thread_id: str | None,
    run_mode: str = "cold_start",
    run_id_override: str | None = None,
    thread_id_override: str | None = None,
) -> dict:
    # A caller that already registered this run under an id of its own (the
    # console does, before this process even starts) passes it in here so
    # the id it is tracking is the same one this process logs and exports
    # under. Left unset, both are minted fresh exactly as before -- the
    # override exists for callers, not for interactive use.
    run_id = run_id_override or f"run-{uuid.uuid4().hex[:12]}"
    thread_id = resume_thread_id or thread_id_override or f"thread-{uuid.uuid4().hex[:12]}"

    ensure_schema()
    checkpointer = await get_async_checkpointer()

    final = await run_pipeline(
        candidate_niches=niches,
        run_id=run_id,
        thread_id=thread_id,
        checkpointer=checkpointer,
        resume=resume_thread_id is not None,
        run_mode=run_mode,
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
    # Export before tearing down the pools, but skip for snapshot-only runs
    if final.get("run_mode") != "snapshot_refresh":
        try:
            from src.export import export_run, export_excel
            from src.tools.export_completeness import gate_before_export

            # The completeness gate, before anything is written. Every empty
            # sheet and blank column this project has shipped came from a
            # node that did not run, or ran against the wrong channels, with
            # nothing between it and the client noticing. This audits the
            # run's own rows against a declared spec, re-runs whatever node
            # fills what is short, and audits again -- so the export is
            # checked rather than assumed.
            #
            # It never blocks the export. A workbook that is 90% populated
            # is still worth handing over WITH its gaps named; refusing to
            # write one would leave the client with nothing at all.
            # The tier budgets this, and the client is quoted it on the
            # card, so it is enforced rather than left at a fixed default
            # that no stated duration accounted for.
            import os as _os

            _gate_budget = _os.environ.get("GATE_BUDGET_SECONDS")
            gate = (
                gate_before_export(export_run_id, budget_seconds=float(_gate_budget))
                if _gate_budget else gate_before_export(export_run_id)
            )
            print("\n" + gate.render())

            path = export_run(export_run_id, thread_id)
            print(f"\nExported to {path}")
            # Curated scope, same as every prior run: 50k subscriber floor,
            # auto-detected dominant category. cap_videos defaults to False —
            # every qualifying video, not a top-500-by-outlier-score preview;
            # the big runs' own ask. all_channels=True (dropping the floor
            # itself) is available directly for a full-dump export if ever
            # needed, opt-in only.
            xlsx_path = export_excel(export_run_id, path / f"{export_run_id}.xlsx")
            print(f"Excel workbook: {xlsx_path}")

            # And then read back what was actually written.
            #
            # The gate above checks the rows it is about to export. This
            # opens the file and reads the cells, which is a different and
            # strictly later question -- every completeness failure this
            # project has shipped came from those two questions quietly
            # being different ones. There is nothing left between this and
            # the client: whatever a person sees when they open the
            # workbook is what this measured.
            #
            # It reports; it does not heal. The gate is where filling
            # happens, and if that did not work, saying COMPLETE a second
            # time is the whole problem repeating itself.
            from src.tools.workbook_verify import verify_run_workbook

            verdict = verify_run_workbook(export_run_id, xlsx_path)
            print("\n" + verdict.render())
            try:
                (path / "verification.txt").write_text(
                    verdict.render() + "\n", encoding="utf-8"
                )
            except Exception:
                pass
            if not verdict.ok:
                print(
                    "\n!! The workbook was written but did NOT verify. "
                    "The gaps above are in the delivered file."
                )
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
    parser.add_argument(
        "--run-id", metavar="RUN_ID",
        help="Use this run_id instead of minting one (for a caller that already registered it)",
    )
    parser.add_argument(
        "--thread-id", metavar="THREAD_ID",
        help="Use this thread_id for a fresh run (ignored with --resume, which supplies its own)",
    )
    parser.add_argument("--augment", action="store_true", help="Augment existing dataset (frontier pre-hydrated from Postgres)")
    parser.add_argument("--snapshot", action="store_true", help="Snapshot-only run: bulk API refresh, no discovery")
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

    if args.augment and args.resume:
        print("Error: --augment and --resume are mutually exclusive. --augment starts a new run_id; --resume continues an old one.")
        raise SystemExit(1)
    if args.snapshot and (args.resume or args.augment):
        print("Error: --snapshot is standalone — it runs bulk refresh only, not alongside --resume or --augment.")
        raise SystemExit(1)
    if args.thread_id and args.resume:
        print("Error: --thread-id and --resume are mutually exclusive -- --resume supplies the thread_id being resumed.")
        raise SystemExit(1)

    run_mode = "snapshot_refresh" if args.snapshot else ("augment" if args.augment else "cold_start")
    final = asyncio.run(
        _run(args.niches, args.resume, run_mode, args.run_id, args.thread_id)
    )

    if args.json:
        print(json.dumps(final, indent=2, default=str))
    else:
        node_names = {log.get("node_name") for log in final.get("node_logs", [])}
        reached_finalize = "finalize_dataset" in node_names
        # v4 multi-niche: show the full cluster
        niches = final.get("selected_niches") or [final.get("selected_niche", "(none)")]
        print(f"Niche{'s' if len(niches) > 1 else ''}: {', '.join(niches)}")
        print(f"Reached finalize_dataset: {reached_finalize}")
        print(f"Channels enriched this run: {final.get('channels_enriched_this_run', 0)}")
        print(f"Budget spent (USD): {final.get('budget_spent_usd', 0.0):.4f}")
        if final.get("errors"):
            print(f"Errors ({len(final['errors'])}):")
            for e in final["errors"][:10]:
                print(f"  ERROR: {e.get('error_type')}: {e.get('message')}")


if __name__ == "__main__":
    main()
