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


def would_exceed(expected_seconds: float) -> bool:
    """True if work expected to take `expected_seconds` cannot finish in time.

    Admission control: starting a five-minute snapshot with two minutes left
    buys a record set the run will never use and still bills for it.
    """
    remaining = remaining_seconds()
    return remaining is not None and expected_seconds > remaining
