"""One bad row must not cost a workbook.

run-b8b0ea1bf0a6 (technology) delivered a file with Channels, Videos,
Shorts, Niches, Success Factors and Failure Factors all empty. The run had
fetched 1,208 videos across 21 channels and the node reported
"channels_hydrated: 21".

What happened, in order:

  YouTube returned, inside one channel's upload scan, a video owned by a
  different channel. videos.channel_id is a foreign key and that owner had
  no row, so the insert raised ForeignKeyViolation.

  The exception escaped the per-channel loop. Every remaining channel was
  skipped, and so was persist_category_tags -- the last statement in the
  block and the only thing that records which rows belong to the run. The
  workbook is built from those tags, so it came out empty.

  channels_hydrated counted len(channels), what the node was handed, so it
  reported 21 while one channel had persisted.

Three separate faults, and the middle one is what turned a single bad row
into an empty deliverable.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import src.tools.hydrate_metadata  # noqa: F401  (registers the module)

# `import src.tools.hydrate_metadata as mod` binds the FUNCTION of that
# name, not the module -- the package re-exports it. Go through sys.modules
# for the file, as the deadline tests already have to.
mod = sys.modules["src.tools.hydrate_metadata"]


def _channel(cid: str, videos: list[dict] | None = None) -> dict:
    return {
        "channel_id": cid,
        "title": f"Channel {cid}",
        "subscriber_count": 100_000,
        "description": "",
        "discovery_method": "keyword",
        "_videos": videos if videos is not None else [
            {"video_id": f"{cid}-v1", "channel_id": cid, "title": "t",
             "view_count": 1, "published_at": "2026-01-01T00:00:00Z"},
        ],
    }


class TestOneChannelsFailureIsOneChannels:
    def test_a_failing_channel_does_not_take_the_others_with_it(self):
        """The whole point. Twenty channels were lost to one bad video."""
        good_a, bad, good_b = _channel("cA"), _channel("cBAD"), _channel("cB")

        def _persist_channel(conn, ch):
            if ch["channel_id"] == "cBAD":
                raise RuntimeError("insert or update on table \"videos\" violates...")

        with patch.object(mod, "persist_channel", side_effect=_persist_channel):
            kept = mod._persist_channels_for_test([good_a, bad, good_b])

        assert kept == ["cA", "cB"]

    def test_only_what_persisted_is_tagged(self):
        """A channel whose row was rolled back has nothing to point at;
        tagging it moves the same foreign-key failure into the tags table."""
        import inspect

        src = inspect.getsource(mod.hydrate_metadata)
        assert "channel_ids=persisted_ids" in src
        assert "channel_ids=[ch[\"channel_id\"] for ch in channels]" not in src

    def test_the_tag_call_is_reached_even_when_a_channel_failed(self):
        """It is the last statement in the block, so an escaping exception
        skipped it entirely -- which is what emptied every sheet."""
        import inspect

        src = inspect.getsource(mod.hydrate_metadata)
        loop_at = src.index("for ch in channels:")
        except_at = src.index("except Exception as exc:", loop_at)
        tag_at = src.index("persist_category_tags(", except_at)
        continue_at = src.index("continue", except_at)
        assert continue_at < tag_at, (
            "the per-channel handler must continue the loop, leaving the "
            "tagging below it reachable"
        )


class TestAVideoIsNeverFiledUnderAChannelThatDoesNotOwnIt:
    def test_a_foreign_owned_video_is_dropped(self):
        import inspect

        src = inspect.getsource(mod.hydrate_metadata)
        assert 'v.get("channel_id") == ch["channel_id"]' in src

    def test_it_is_not_re_attributed(self):
        """Rewriting the owner would file another creator's video under this
        channel, and every per-channel statistic in the workbook is computed
        over exactly these rows."""
        import inspect

        src = inspect.getsource(mod.hydrate_metadata)
        assert 'vid["channel_id"] = ch["channel_id"]' not in src

    def test_the_drop_is_counted_rather_than_silent(self):
        import inspect

        src = inspect.getsource(mod.hydrate_metadata)
        assert "foreign_owned_videos_dropped" in src
        assert "hydrate_dropped_foreign_owned_videos" in src


class TestTheCountReportsWhatLanded:
    def test_channels_hydrated_counts_what_persisted(self):
        """It counted what the node was handed, so it read 21 on a run where
        one channel persisted. A count that cannot go down is not a
        measurement."""
        import inspect

        src = inspect.getsource(mod.hydrate_metadata)
        assert '"channels_hydrated": len(persisted_ids)' in src
        assert '"channels_attempted": len(channels)' in src

    def test_it_survives_a_connection_that_never_opened(self):
        """persisted_ids is read by the summary, so it has to exist even
        when the try around it never ran."""
        import inspect

        src = inspect.getsource(mod.hydrate_metadata)
        decl = src.index("persisted_ids: list[str] = []")
        try_at = src.index("conn = get_connection()")
        assert decl < try_at
