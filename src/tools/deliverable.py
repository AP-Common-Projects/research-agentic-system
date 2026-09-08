"""What "reaches the deliverable" means -- asked once, answered once.

Two different rules were deciding the same thing. The workbook ships
channels whose real `subscriber_count` clears the client's floor (see
export._floor: "the client's rule is about the real count"). The seven
per-channel enrichment nodes selected on `meets_subscriber_floor`, a flag
that is ALSO true for sub-floor channels that tripped a breakout or
thriving override in compute_subscriber_floor.

Those are not the same population, and not by a little. Measured across
the organic runs in the store: 41% of hydrated channels carry the flag,
15% clear the real floor. On run-dd2dbdbc3080 the enrichment chain ran on
65 channels and the workbook shipped 24 -- so roughly two thirds of the
run's LLM latency, which is the thing that ends these runs, was spent on
rows the export then filtered out.

The flag is not wrong; it is a genuine signal about small channels worth
watching, and it stays. It is just not the question "will this channel be
in the file", and that is the only question the enrichment gate is asking.

This module is that question. Both the export and every node call it, so
they cannot drift apart again.
"""

from __future__ import annotations

from typing import Any


def deliverable_floor() -> int:
    """The subscriber count a channel must have to reach the workbook."""
    from src.config import get_config

    return int(get_config().harness.subscriber_floor)


def eligible_sql(alias: str | None = "c") -> str:
    """SQL predicate selecting channels that can reach the workbook.

    Interpolated rather than parameterised because the floor is an int
    read from config, and the surrounding queries build their WHERE
    clauses as strings already.
    """
    column = f"{alias}.subscriber_count" if alias else "subscriber_count"
    return f"{column} >= {deliverable_floor()}"


#: Share of hydrated channels that clear the 50k floor. Measured over
#: every organic run in the store on 2026-09-08 -- the `run-broad-*` rows
#: are excluded because they were seeded from curated channel lists and
#: pass at 100%, which is not what discovery does:
#:
#:     run-27112d3c19bc  3268 hydrated   296 over floor    9%
#:     run-44e3fb775db0  2738            310             11%
#:     run-2f44beffc241   932            203             22%
#:     run-019f20e21145   832            192             23%
#:     run-3c7dd144a253   565             60             11%
#:     run-6a4bd49777b7   360             32              9%
#:     run-5234571186dc   285             59             21%
#:     run-c6c45a3e91b2   277             80             29%
#:     run-dd2dbdbc3080   100             24             24%
#:     run-bc5226fb2e06   100             28             28%
#:                       ----           ----
#:                       9974           1543           15.5%
_FLOOR_YIELD = 0.20

#: And the share of THOSE that survive the export's own filters. The floor
#: is not the last one: the workbook is also scoped to the run's dominant
#: category, falling back to own_only when no category resolves. Measured
#: the same day, over the same runs, by asking export.workbook_scope:
#:
#:     run-2f44beffc241   203 over floor   182 in file   90%
#:     run-5234571186dc    59              56           95%
#:     run-bc5226fb2e06    28              25           89%
#:     run-6a4bd49777b7    32              27           84%
#:     run-dd2dbdbc3080    24              17           71%
#:     run-3c7dd144a253    60              36           60%
#:     run-c6c45a3e91b2    80              44           55%
#:     run-019f20e21145   192              89           46%
#:
#: This is discovery drifting off-topic, not a filter misbehaving: on
#: run-dd2dbdbc3080 eleven of the twenty-four floor-passing channels
#: classified as lifestyle and three as education, on an education run.
#: The export is right to drop them; the budget has to pay for the drift.
_SCOPE_YIELD = 0.75

#: What a run must hydrate to put one row in the file: 15% of hydrated
#: channels clear the floor and three quarters of those survive the scope.
DISCOVERY_YIELD = _FLOOR_YIELD * _SCOPE_YIELD

#: The same two rates at the worst a tier is sized to survive, used for
#: the hydration CEILING and for checking each tier's stated minimum. The
#: pooled end-to-end figure across the organic runs is 9.4%, dragged down
#: by the two very wide augment runs at 6%; the focused runs -- the shape
#: a depth tier actually describes -- cluster at 16-25%.
_FLOOR_YIELD_PESSIMISTIC = 0.155
_SCOPE_YIELD_PESSIMISTIC = 0.60
DISCOVERY_YIELD_PESSIMISTIC = _FLOOR_YIELD_PESSIMISTIC * _SCOPE_YIELD_PESSIMISTIC

#: Never hydrate more than this multiple of the delivery target, however
#: bad the yield gets. Without it a topic where nothing clears the floor
#: would hydrate without limit chasing a target it cannot reach.
#:
#: Twelve, not eight: at the pessimistic end-to-end rate a run needs
#: nearly eleven hydrations per delivered row, and a ceiling that bit
#: before the target could be reached would reinstate the whole bug at a
#: different number. Hydration is the cheap stage -- 3.3 seconds and 3.3
#: quota units a channel -- so a generous ceiling costs little, and the
#: run stops on the target long before it in the normal case.
MAX_YIELD_MULTIPLE = 12


def hydration_ceiling(delivery_target: int, yield_rate: float | None = None) -> int:
    """Channels a run may hydrate to deliver `delivery_target` rows.

    The two numbers were the same number, and that is the bug: the cap
    bounded hydration while the card quoted the workbook. A Standard run
    capped at 100 hydrated 100, cleared the floor with 24, and shipped 17.

    Sized on the PESSIMISTIC rate, because this is a ceiling rather than a
    plan. The run stops when it holds the target; this only has to be
    large enough that it never stops the run short of it.
    """
    target = int(delivery_target or 0)
    if target <= 0:
        return 0
    rate = DISCOVERY_YIELD_PESSIMISTIC if yield_rate is None else max(1e-6, float(yield_rate))
    return min(int(target * MAX_YIELD_MULTIPLE), max(target, int(round(target / rate))))


def run_ceilings(harness: Any = None) -> tuple[int, int]:
    """`(delivery_target, hydration_ceiling)` for a run, 0 meaning uncapped.

    The delivery target is MAX_CHANNELS_PER_RUN and now means what the
    depth card says it means: rows in the finished workbook. The hydration
    ceiling is what actually bounds API spend, and is derived unless
    MAX_HYDRATED_CHANNELS_PER_RUN sets it explicitly.
    """
    if harness is None:
        from src.config import get_config

        harness = get_config().harness
    target = int(getattr(harness, "max_channels_per_run", 0) or 0)
    explicit = int(getattr(harness, "max_hydrated_channels_per_run", 0) or 0)
    if explicit > 0:
        # Never below the target. Hydrating fewer channels than the run has
        # promised to deliver cannot produce the promise, whatever the
        # ceiling says, so a smaller figure is a misconfiguration rather
        # than a tighter budget.
        return target, max(target, explicit)
    return target, hydration_ceiling(target)
