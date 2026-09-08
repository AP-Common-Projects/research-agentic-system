"""Bounded-concurrency fan-out for the per-channel LLM stages.

Every per-channel node was a serial `for` loop around one LLM call, and
that -- not money, not API quota -- is what ends these runs. Measured on
run-dd2dbdbc3080, a 4-hour Standard:

    extract_success_failure_factors   56 channels   2872s   51s each
    describe_video_titles             57 batches    1329s   23s each
    classify_channel                  ~50           1192s   16s each
    populate_taxonomy_dimensions      50             717s   14s each
    populate_shared_fields            50             474s   9.5s each

That is 6,584 of the run's 8,217 seconds spent waiting on a socket, for
$1.30 of tokens against a $9 ceiling. The run stopped on the clock with
its wallet almost untouched, which is exactly the shape the client saw:
"$1.3, much less than what we assumed".

Latency-bound work parallelises; the calls are independent, the provider
takes them concurrently, and `_create_retry_decorator` in llm.client
already treats 429 as retryable with backoff, so overshooting the
provider's rate costs a wait rather than a failure.

Two properties the callers depend on:

  * **Results come back in input order.** A node that writes rows in a
    different order than it read them is a node whose output depends on
    thread scheduling, and this codebase has had enough of those.
  * **Failures are values, not exceptions.** Each entry carries its own
    error, so one channel's bad response cannot end the batch -- the same
    per-item fault isolation the serial loops had.

`should_stop` is consulted before each SUBMIT, so a node that runs out of
its write-up budget stops starting work immediately and only drains what
is already in flight. Overshoot is bounded by the worker count rather
than by one channel, which is the honest cost of the speed-up.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Sequence, TypeVar

import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")
R = TypeVar("R")

#: Used when nothing configures it -- see HarnessConfig.llm_concurrency.
DEFAULT_WORKERS = 8


def worker_count(override: int | None = None) -> int:
    """How many calls may be in flight, from config unless told otherwise."""
    if override is not None:
        return max(1, int(override))
    try:
        from src.config import get_config

        return max(1, int(get_config().harness.llm_concurrency or DEFAULT_WORKERS))
    except Exception:
        # An unreadable config must not stop the work, and must not
        # silently make it serial either.
        return DEFAULT_WORKERS


def map_llm(
    items: Iterable[T],
    call: Callable[[T], R],
    *,
    workers: int | None = None,
    should_stop: Callable[[], bool] | None = None,
    label: str = "",
) -> list[tuple[T, R | None, BaseException | None]]:
    """`call` every item with bounded concurrency; results in input order.

    Returns one `(item, result, error)` triple per item that was STARTED.
    Items skipped because `should_stop` went true are simply absent, which
    is what the serial `break` did.
    """
    pending_items: Sequence[T] = list(items)
    if not pending_items:
        return []

    n = worker_count(workers)
    out: list[tuple[T, R | None, BaseException | None]] = []

    # One worker is the serial path, exactly: no pool, no threads, and the
    # same ordering. Tests and single-item batches take it.
    if n <= 1 or len(pending_items) == 1:
        for item in pending_items:
            if should_stop is not None and should_stop():
                break
            try:
                out.append((item, call(item), None))
            except Exception as exc:  # noqa: BLE001 - reported per item
                out.append((item, None, exc))
        return out

    source = iter(pending_items)
    inflight: deque[tuple[T, Any]] = deque()
    stopped = False

    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="llm") as pool:
        def refill() -> None:
            nonlocal stopped
            while not stopped and len(inflight) < n:
                if should_stop is not None and should_stop():
                    stopped = True
                    return
                try:
                    item = next(source)
                except StopIteration:
                    return
                inflight.append((item, pool.submit(call, item)))

        refill()
        while inflight:
            item, future = inflight.popleft()
            try:
                out.append((item, future.result(), None))
            except Exception as exc:  # noqa: BLE001 - reported per item
                out.append((item, None, exc))
            refill()

    if label:
        failed = sum(1 for _, _, err in out if err is not None)
        logger.debug(
            "llm_fanout",
            label=label, workers=n, started=len(out),
            of=len(pending_items), failed=failed,
        )
    return out
