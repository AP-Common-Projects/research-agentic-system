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


#: Share of hydrated channels that clear the floor and can reach the file.
#:
#: Measured over every organic run in the store on 2026-09-08 -- the
#: `run-broad-*` rows are excluded because they were seeded from curated
#: channel lists and pass at 100%, which is not what discovery does:
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
#:
#: 0.20 sits between the pooled 15.5% and the ~24% the recent, smaller,
#: better-targeted runs achieve. Setting it too high is the failure that
#: matters: the run would stop hydrating before it had enough qualifying
#: channels and deliver under its stated band, which is the bug this whole
#: change exists to remove. Too low only means the ceiling is never
#: reached, and the run stops on the target instead -- exactly as intended.
DISCOVERY_YIELD = 0.20

#: Never hydrate more than this multiple of the delivery target, however
#: bad the yield gets. Without it a topic where nothing clears the floor
#: would hydrate without limit chasing a target it cannot reach.
MAX_YIELD_MULTIPLE = 8


def hydration_ceiling(delivery_target: int, yield_rate: float | None = None) -> int:
    """Channels a run may hydrate to deliver `delivery_target` of them.

    The two numbers were the same number, and that is the bug: the cap
    bounded hydration while the card quoted the workbook. A Standard run
    capped at 100 hydrated 100, cleared the floor with 24 and shipped 17.
    """
    target = int(delivery_target or 0)
    if target <= 0:
        return 0
    rate = DISCOVERY_YIELD if yield_rate is None else max(1e-6, float(yield_rate))
    return min(int(target * MAX_YIELD_MULTIPLE), max(target, int(round(target / rate))))


def run_ceilings(harness: Any = None) -> tuple[int, int]:
    """`(delivery_target, hydration_ceiling)` for a run, 0 meaning uncapped.

    The delivery target is MAX_CHANNELS_PER_RUN and now means what the
    depth card says it means: floor-passing channels in the finished
    workbook. The hydration ceiling is what actually bounds API spend, and
    is derived unless MAX_HYDRATED_CHANNELS_PER_RUN sets it explicitly.
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
