"""Re-run enrichment for one run's channels, using the fixed, scoped nodes.

The automotive run finished with 0 of its 76 floor-passing channels
classified, because every enrichment node's eligibility query was global:
classify_channel's LIMIT 50 selected 50 channels of which 7 belonged to the
run. Everything downstream followed -- no primary_niche_id, so the Niches,
Success Factors and Failure Factors sheets were empty and the discovery
graph filed all 76 under "Unclassified".

The nodes are fixed, but the run is over, so nothing will re-enter them for
that data. This drives the same nodes against the same channels with the
run's own scope, which is what the fixed pipeline would now do by itself.
It exists to repair a run that predates the fix AND to prove the fix on
real data before another run is paid for.

Usage:
    python scripts/gapfill/backfill_run_enrichment.py <run_id> [--dry-run]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.db.connection import get_connection, put_connection  # noqa: E402


def run_channel_ids(run_id: str) -> list[str]:
    """Every channel this run discovered, floor-passing or not.

    Not filtered to the floor here: each node applies its own eligibility,
    and the floor is one of the things a re-run might legitimately see
    differently if the threshold has since been changed.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT channel_id FROM channels WHERE first_discovered_run_id = %s",
            (run_id,),
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        put_connection(conn)


def fill_rates(run_id: str) -> dict[str, tuple[int, int]]:
    """How populated the run's floor-passing channels are, column by column."""
    cols = [
        "primary_niche_id", "channel_size_bucket", "country_code", "region",
        "primary_language_code", "language_confidence", "evergreen_score",
        "engagement_score", "is_likely_news", "first_video_published_at",
        "primary_topic",
    ]
    sel = ", ".join(f"COUNT({c})" for c in cols)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT COUNT(*), {sel} FROM channels
                WHERE first_discovered_run_id = %s AND meets_subscriber_floor""",
            (run_id,),
        )
        row = cur.fetchone()
        return {c: (row[i + 1], row[0]) for i, c in enumerate(cols)}
    finally:
        put_connection(conn)


def report(label: str, rates: dict[str, tuple[int, int]]) -> None:
    print(f"\n  {label}")
    for col, (n, total) in rates.items():
        bar = "#" * int(20 * n / total) if total else ""
        print(f"    {col:26} {n:4}/{total}  {bar}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    run_id = sys.argv[1]
    dry = "--dry-run" in sys.argv

    ids = run_channel_ids(run_id)
    if not ids:
        print(f"  no channels discovered by {run_id}")
        raise SystemExit(1)

    print(f"  {run_id}: {len(ids)} discovered channels")
    report("before", fill_rates(run_id))
    if dry:
        return

    # The state a node would have seen inside the run. scope_channel_ids is
    # what src/tools/run_scope.py reads first, so every node restricts
    # itself to exactly these channels.
    state = {
        "run_id": run_id,
        "thread_id": f"backfill-{run_id}",
        "discovered_channel_ids": ids,
        "scope_channel_ids": ids,
        "hydrated_channel_ids": set(),
    }

    from src.nodes.assign_cohorts import assign_cohorts
    from src.nodes.classify_channel import classify_channel
    from src.nodes.extract_metadata_signals import extract_metadata_signals
    from src.nodes.extract_success_failure_factors import extract_success_failure_factors
    from src.nodes.populate_shared_fields import populate_shared_fields
    from src.nodes.populate_taxonomy_dimensions import populate_taxonomy_dimensions
    from src.nodes.resolve_first_video_date import resolve_first_video_date
    from src.nodes.resolve_geo_language import resolve_geo_language
    from src.tools.signal_scoring import score_signals

    # classify_channel and extract_success_failure_factors take 50 per call
    # and are the expensive ones, so they are looped until they stop making
    # progress rather than run once. The rest are single-pass.
    def loop(node, name: str, key: str, limit: int = 12) -> None:
        for i in range(limit):
            t0 = time.monotonic()
            out = node(dict(state))
            logs = out.get("node_logs") or [{}]
            summary = (logs[-1] or {}).get("input_summary", {})
            n = summary.get(key, 0) or 0
            print(f"    {name} pass {i + 1}: {key}={n} "
                  f"({time.monotonic() - t0:.0f}s)", flush=True)
            if not n:
                return

    print("\n  running the fixed enrichment chain")
    for node, name in (
        (resolve_geo_language, "resolve_geo_language"),
        (extract_metadata_signals, "extract_metadata_signals"),
        (score_signals, "score_signals"),
    ):
        t0 = time.monotonic()
        node(dict(state))
        print(f"    {name} ({time.monotonic() - t0:.0f}s)", flush=True)

    loop(classify_channel, "classify_channel", "classified")
    loop(resolve_first_video_date, "resolve_first_video_date", "resolved")
    loop(extract_success_failure_factors, "extract_success_failure_factors", "extracted")

    for node, name in (
        (populate_taxonomy_dimensions, "populate_taxonomy_dimensions"),
        (populate_shared_fields, "populate_shared_fields"),
        (assign_cohorts, "assign_cohorts"),
    ):
        t0 = time.monotonic()
        node(dict(state))
        print(f"    {name} ({time.monotonic() - t0:.0f}s)", flush=True)

    report("after", fill_rates(run_id))
    print("\n  re-export with:  python -c \""
          "from src.export import export_excel; export_excel('%s')\"" % run_id)


if __name__ == "__main__":
    main()
