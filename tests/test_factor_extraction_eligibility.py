"""extract_success_failure_factors must not loop forever on a real zero-factor
result.

Eligibility used to be "no row exists in channel_success_factors for this
channel". A channel the model genuinely finds zero success factors for gets
no row either way -- a real, negative result and a channel nobody has
looked at yet were indistinguishable. That channel stayed eligible forever,
and the node's own internal `while eligible:` loop kept re-selecting and
re-billing it every pass.

Caught live: an automotive backfill spent over four hours re-running one
channel's LLM call before being killed by hand. Its 1000-channel safety cap
would eventually have stopped it, but only after roughly 1000 billed
attempts at the same channel.

The fix is a dedicated timestamp, set once an attempt has been made
regardless of what it found, and eligibility reads that instead of row
existence.
"""

from __future__ import annotations

import inspect

from src.nodes import extract_success_failure_factors as mod


class TestEligibilityUsesAnExplicitMarker:
    def test_eligibility_no_longer_keys_on_row_existence(self):
        """The exact bug: NOT EXISTS on the factor table cannot tell a real
        zero-result apart from an unattempted channel."""
        src = inspect.getsource(mod.extract_success_failure_factors)
        assert "NOT EXISTS (SELECT 1 FROM channel_success_factors" not in src

    def test_eligibility_reads_the_checked_at_marker(self):
        src = inspect.getsource(mod.extract_success_failure_factors)
        assert "success_failure_factors_checked_at IS NULL" in src

    def test_the_marker_is_a_real_schema_column(self):
        from src.db.schema import MIGRATIONS

        assert any(
            "success_failure_factors_checked_at" in stmt for stmt in MIGRATIONS
        ), "the column must be created by a migration, not assumed to exist"


class TestASuccessfulAttemptIsAlwaysMarked:
    """Whether the model found factors or not, the channel must be marked --
    that is what stops it being re-selected on the next pass."""

    def test_the_success_path_marks_the_channel(self):
        src = inspect.getsource(mod.extract_success_failure_factors)
        # The mark call sits after `extracted += 1`, on the path every
        # successfully-processed channel takes regardless of factor count.
        idx = src.index("extracted += 1")
        tail = src[idx: idx + 200]
        assert "_mark_checked" in tail

    def test_a_local_processing_error_is_still_marked(self):
        """A channel whose own data is malformed (a NULL where a float is
        expected) fails the same way on every future attempt too. Leaving
        it unmarked recreates the same loop through a different door."""
        src = inspect.getsource(mod.extract_success_failure_factors)
        # The outer except (channel-local failure, not the LLM call itself)
        # must also reach _mark_checked.
        boundary = src.index("if total_eligible_seen >= max_channels")
        outer_except = src.rindex("except Exception as exc:", 0, boundary)
        segment = src[outer_except:boundary]
        assert "_mark_checked" in segment

    def test_a_transient_llm_failure_is_not_marked(self):
        """The opposite case: a network blip or a malformed LLM response is
        not a property of the channel, and marking it here would convert a
        retryable failure into a permanent skip."""
        src = inspect.getsource(mod.extract_success_failure_factors)
        # complete_json now, which retries a reply that is not JSON before
        # giving up -- the call itself is still the anchor.
        call_start = src.index("complete_json(")
        except_start = src.index("except Exception as exc:", call_start)
        # The next statement after this except's body starts is `continue`
        # (via the errors.append/continue block); _mark_checked must not
        # appear between the try and the following except's own boundary.
        next_except = src.index("except Exception as exc:", except_start + 10)
        segment = src[except_start:next_except]
        assert "_mark_checked" not in segment


class TestMarkCheckedHelper:
    def test_it_exists_and_writes_the_column(self):
        import inspect as _inspect

        assert hasattr(mod, "_mark_checked")
        src = _inspect.getsource(mod._mark_checked)
        assert "success_failure_factors_checked_at" in src
        assert "channel_id" in src
