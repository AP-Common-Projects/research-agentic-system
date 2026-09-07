"""The run's wall-clock ceiling, and the checks that make it precise.

Every other governor in this harness bounds work. This one bounds time, and
it exists because the console names each depth by duration -- a client
picking "a 30-minute run" is told what they are buying, and that has to be
true rather than projected.

A deadline enforced in only one place is not precise. Checked solely in
check_saturation, which runs once per branch cycle after discovery,
hydration and classification, a 30-minute tier stopped at whatever the
first full cycle happened to cost -- measured at 55+ minutes, and the
overshoot grows with the tier. So the deadline is enforced at three depths:

  admission   Before an expensive node starts, `passed()` lets it no-op
              rather than begin work that cannot land. Cheap, and it wastes
              nothing.

  iteration   Inside the two loops that run for minutes at a time -- the
              Bright Data snapshot poll and the per-channel classifier --
              so an in-flight node yields near the limit rather than at its
              own natural end.

  termination check_saturation turns the deadline into the same clean exit
              an exhausted budget takes: force-saturate, compact, finalize,
              export what the run reached.

Together those bound overshoot to one poll interval or one channel, rather
than to one whole cycle.

The clock is per-process, not read from run state: a resumed run should get
its own full window rather than inherit an original start that would trip
the ceiling on its first check.
"""

from __future__ import annotations

import time

from src.config import get_config

#: When this process began. Module import happens at graph construction, a
#: few seconds into the process at most.
_PROCESS_STARTED_AT = time.monotonic()

#: Share of the window given to research (discovery, hydration,
#: classification). The remainder is reserved for the write-up chain, which
#: is bounded by what research collected. Set from the automotive run, where
#: the write-up would have been a few minutes against a 30-minute window;
#: a fifth is generous, and being generous here costs channels rather than
#: correctness.
RESEARCH_SHARE = 0.8


def run_elapsed_seconds() -> float:
    """Wall-clock seconds since this process started."""
    return time.monotonic() - _PROCESS_STARTED_AT


def deadline_seconds() -> int:
    """The configured ceiling, or 0 when uncapped."""
    try:
        return int(get_config().harness.run_deadline_seconds or 0)
    except Exception:
        # A config that cannot be read must not stop a run that was going to
        # succeed; uncapped is the pre-existing behaviour.
        return 0


def remaining_seconds() -> float | None:
    """Seconds left, or None when no deadline is set. Never negative."""
    ceiling = deadline_seconds()
    if ceiling <= 0:
        return None
    return max(0.0, ceiling - run_elapsed_seconds())


def passed() -> bool:
    """True once the run has spent its allotted wall-clock time."""
    remaining = remaining_seconds()
    return remaining is not None and remaining <= 0


def research_passed(state: dict | None = None) -> bool:
    """True once the RESEARCH half of the window is spent.

    A run has two phases and only one of them is open-ended. Discovery,
    hydration and classification scale with whatever discovery finds and
    are what the deadline exists to bound. The write-up that follows --
    success and failure factors, video descriptions, taxonomy dimensions,
    cohorts -- is bounded by the data already collected, and skipping it
    is what produced workbooks with three empty sheets.

    So the research phase stops early enough to leave the write-up room to
    finish inside the same stated duration, rather than the write-up being
    dropped to make the duration. A tier that promises 30 minutes still
    means 30 minutes; it just spends the last part of them finishing the
    deliverable instead of looking for more channels.
    """
    if state is not None and state.get("healing"):
        # The export gate re-runs classify_channel to fill primary_niche_id,
        # and it does so after the run's window has closed -- which is the
        # only time it is ever needed. Asking the run deadline there meant
        # the node broke on its first channel and reported 0 classified, so
        # the single column three sheets depend on could never be healed.
        # The gaming run of 2026-09-07 shipped Niches, Success Factors and
        # Failure Factors empty for exactly that reason.
        heal_deadline = state.get("heal_deadline_monotonic")
        if heal_deadline is None:
            return False
        return time.monotonic() >= float(heal_deadline)

    ceiling = deadline_seconds()
    if ceiling <= 0:
        return False
    return run_elapsed_seconds() >= ceiling * RESEARCH_SHARE


def writeup_passed(state: dict | None = None) -> bool:
    """True when the write-up chain should stop taking on new work.

    research_passed() bounds discovery; this bounds what follows it. Until
    now nothing did: every enrichment and write-up node ran to completion
    however long that took, which is how a one-hour crime run took 2h34m --
    research yielded on time at 48 minutes and the write-up then spent an
    hour and three quarters unbounded.

    The check is per-iteration, so overshoot is one channel or one batch
    rather than one whole node.

    `state` matters. The export gate re-runs these very nodes AFTER the
    deadline, deliberately, to fill what the run ran out of time for -- so
    a node that simply asked passed() would turn the completeness gate into
    a no-op and trade every missing column for the schedule.

    Healing is therefore exempt from the RUN deadline, but not from every
    deadline: it carries its own budget, and that budget has to be
    enforceable in the same place. Checked only between node calls it is
    not -- one populate_crime_metadata call is fifty videos, and a batch
    that hits a slow retry path can hold it for minutes. A 30-minute heal
    budget overran to 40 that way. So healing passes its own deadline down
    and is measured against that instead.
    """
    if state is not None and state.get("healing"):
        heal_deadline = state.get("heal_deadline_monotonic")
        if heal_deadline is None:
            return False
        return time.monotonic() >= float(heal_deadline)
    return passed()


def would_exceed(expected_seconds: float) -> bool:
    """True if work expected to take `expected_seconds` cannot finish in time.

    Admission control: starting a five-minute snapshot with two minutes left
    buys a record set the run will never use and still bills for it.
    """
    remaining = remaining_seconds()
    return remaining is not None and expected_seconds > remaining
