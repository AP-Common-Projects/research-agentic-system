"""The per-channel stages run several calls at once, and still in order.

run-dd2dbdbc3080 spent 6,584 of its 8,217 node-seconds waiting on serial
LLM completions worth $1.30 of tokens against a $9 ceiling. The run ended
on the clock with its wallet almost untouched -- which is what the client
saw as "cost $1.3, much less than what we assumed".

extract_success_failure_factors alone measured 51 seconds a channel. At
250 channels that one stage is 3h30m, and no Standard window holds it.
"""

from __future__ import annotations

import pathlib
import threading
import time

import pytest

from src.llm.concurrent import DEFAULT_WORKERS, map_llm, worker_count


class TestOrderAndFaults:
    def test_results_come_back_in_input_order(self):
        """A node that writes rows in a different order than it read them
        is a node whose output depends on thread scheduling."""
        items = list(range(40))
        out = map_llm(items, lambda i: i * 2, workers=8)
        assert [it for it, _, _ in out] == items
        assert [v for _, v, _ in out] == [i * 2 for i in items]

    def test_a_failure_is_a_value_not_an_exception(self):
        """Per-item fault isolation, the same the serial loops had: one
        channel's bad response must not end the batch."""
        out = map_llm(range(4), lambda i: 1 // (i - 2), workers=4)
        errors = {it: err for it, _, err in out}
        assert isinstance(errors[2], ZeroDivisionError)
        assert errors[0] is None and errors[3] is None
        assert len(out) == 4

    def test_an_empty_batch_does_nothing(self):
        assert map_llm([], lambda i: i) == []

    def test_one_worker_is_the_serial_path(self):
        seen = []
        out = map_llm(range(6), lambda i: seen.append(i) or i, workers=1)
        assert seen == list(range(6))
        assert [v for _, v, _ in out] == list(range(6))

    def test_a_single_item_never_starts_a_pool(self):
        threads_before = threading.active_count()
        map_llm([1], lambda i: i, workers=8)
        assert threading.active_count() == threads_before


class TestItActuallyOverlaps:
    def test_sixteen_slow_calls_do_not_take_sixteen_times_as_long(self):
        started = time.monotonic()
        map_llm(range(16), lambda i: time.sleep(0.05), workers=8)
        elapsed = time.monotonic() - started
        # Serial would be 0.80s; eight at a time is ~0.10s. Half of serial
        # is a deliberately loose bar -- this asserts that the calls
        # overlap at all, not that a CI box hits a particular speed.
        assert elapsed < 0.40, elapsed

    def test_the_width_is_bounded(self):
        live = 0
        peak = 0
        lock = threading.Lock()

        def _call(_):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.02)
            with lock:
                live -= 1

        map_llm(range(30), _call, workers=4)
        assert peak <= 4, peak


class TestStoppingEarly:
    def test_should_stop_prevents_further_submissions(self):
        """The write-up deadline. Overshoot is bounded by the worker count
        rather than by one channel -- the honest cost of the speed-up."""
        out = map_llm(range(100), lambda i: i, workers=4, should_stop=lambda: True)
        assert out == []

    def test_what_was_started_is_still_returned(self):
        calls = []

        def _call(i):
            calls.append(i)
            return i

        out = map_llm(range(50), _call, workers=2,
                      should_stop=lambda: len(calls) >= 4)
        assert 0 < len(out) < 50
        assert [it for it, _, _ in out] == list(range(len(out)))

    def test_a_never_stopping_predicate_runs_everything(self):
        out = map_llm(range(20), lambda i: i, workers=8, should_stop=lambda: False)
        assert len(out) == 20


class TestTheWorkerCountIsConfigured:
    def test_it_reads_the_harness_setting(self):
        from src.config import get_config

        assert worker_count() == get_config().harness.llm_concurrency

    def test_an_override_wins(self):
        assert worker_count(3) == 3

    def test_it_never_returns_zero(self):
        assert worker_count(0) == 1
        assert worker_count(-5) == 1

    def test_the_default_is_stated_once(self):
        assert DEFAULT_WORKERS >= 2


class TestEveryExpensiveStageUsesIt:
    """The five stages that were 80% of the reference run's wall clock."""

    STAGES = [
        "extract_success_failure_factors",
        "classify_channel",
        "describe_video_titles",
        "populate_taxonomy_dimensions",
        "populate_shared_fields",
        "populate_crime_metadata",
    ]

    @pytest.mark.parametrize("name", STAGES)
    def test_the_stage_fans_its_calls_out(self, name):
        code = pathlib.Path(f"src/nodes/{name}.py").read_text(encoding="utf-8")
        assert "map_llm(" in code, name

    @pytest.mark.parametrize("name", STAGES)
    def test_the_stage_still_honours_its_deadline(self, name):
        code = pathlib.Path(f"src/nodes/{name}.py").read_text(encoding="utf-8")
        assert "should_stop=" in code, name

    def test_the_tiers_are_sized_on_a_conservative_share_of_it(self):
        """A tier sized on the nominal width would overrun the moment the
        fan-out did not achieve it."""
        from src.api import depth as depth_mod
        from src.config import get_config

        assert depth_mod._EFFECTIVE_CONCURRENCY < get_config().harness.llm_concurrency
