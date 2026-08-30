"""get_channel_first_video_published_at — the channel's real first-ever
upload date, found by paginating the uploads playlist to its true last
page. get_channel_videos only ever fetches one page (<=50 items, newest
first, no oldest-first sort exists), so for any channel with more than 50
uploads ever, deriving "first video" from that one page is wrong — verified
off by over a decade on a real channel with a long upload history.
"""

from __future__ import annotations

from unittest.mock import patch

from src.tools.youtube_api import YouTubeAPIClient


def _client() -> YouTubeAPIClient:
    return YouTubeAPIClient()


class TestGetChannelFirstVideoPublishedAt:
    def test_single_page_returns_the_last_items_date(self):
        client = _client()
        channels_resp = {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUxyz"}}}]}
        page = {
            "items": [
                {"snippet": {"publishedAt": "2026-01-03T00:00:00Z"}},
                {"snippet": {"publishedAt": "2026-01-02T00:00:00Z"}},
                {"snippet": {"publishedAt": "2026-01-01T00:00:00Z"}},
            ]
            # no nextPageToken — this is the only page
        }
        with patch.object(client, "_get", side_effect=[channels_resp, page]):
            result = client.get_channel_first_video_published_at("UCabc")

        # The LAST item on the (only, therefore last) page is the oldest —
        # playlistItems returns newest-first.
        assert result == "2026-01-01T00:00:00Z"

    def test_paginates_to_the_true_last_page(self):
        """The whole point of this method: a channel with more than one
        page of uploads must not stop at page 1 (which is what
        get_channel_videos does) — it must walk to the actual end."""
        client = _client()
        channels_resp = {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUxyz"}}}]}
        page1 = {
            "items": [{"snippet": {"publishedAt": "2026-06-01T00:00:00Z"}}],
            "nextPageToken": "TOKEN2",
        }
        page2 = {
            "items": [{"snippet": {"publishedAt": "2013-09-12T00:00:00Z"}}],
            # no nextPageToken — this really is the last page
        }
        with patch.object(client, "_get", side_effect=[channels_resp, page1, page2]):
            result = client.get_channel_first_video_published_at("UCabc")

        assert result == "2013-09-12T00:00:00Z", (
            "must be the last page's date, not page 1's — the bug this method exists to fix"
        )

    def test_channel_with_no_uploads_playlist_returns_none(self):
        client = _client()
        with patch.object(client, "_get", return_value={"items": [{"contentDetails": {}}]}):
            assert client.get_channel_first_video_published_at("UCabc") is None

    def test_channel_not_found_returns_none(self):
        client = _client()
        with patch.object(client, "_get", return_value={"items": []}):
            assert client.get_channel_first_video_published_at("UCabc") is None

    def test_stops_at_the_page_safety_ceiling(self):
        """A pathological channel must not turn one lookup into an
        unbounded sequence of API calls."""
        client = _client()
        channels_resp = {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUxyz"}}}]}

        def infinite_pages(*_args, **_kwargs):
            return {
                "items": [{"snippet": {"publishedAt": "2020-01-01T00:00:00Z"}}],
                "nextPageToken": "ALWAYS_MORE",
            }

        with patch.object(client, "_get", side_effect=[channels_resp] + [infinite_pages()] * 5):
            result = client.get_channel_first_video_published_at("UCabc", max_pages=5)

        assert result == "2020-01-01T00:00:00Z"

    def test_terminated_channel_404_is_handled_gracefully(self):
        import httpx

        client = _client()
        channels_resp = {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUxyz"}}}]}
        request = httpx.Request("GET", "https://example.com")
        response = httpx.Response(404, request=request)
        error = httpx.HTTPStatusError("not found", request=request, response=response)

        with patch.object(client, "_get", side_effect=[channels_resp, error]):
            result = client.get_channel_first_video_published_at("UCabc")

        assert result is None


class TestGetChannelUploadBreakdown:
    """statistics.videoCount is one lifetime number with no type split, so
    the breakdown comes from YouTube's auto-generated per-type playlists,
    addressed by swapping the uploads playlist's UU prefix."""

    def test_queries_the_four_prefixed_playlists_and_returns_totals(self):
        client = _client()
        seen: list[str] = []

        def fake_get(endpoint, params):
            seen.append(params["playlistId"])
            return {"pageInfo": {"totalResults": {
                "UUabc": 2761, "UULFabc": 1759, "UUSHabc": 942, "UULVabc": 60,
            }[params["playlistId"]]}}

        with patch.object(client, "_get", side_effect=fake_get):
            out = client.get_channel_upload_breakdown("UCabc")

        assert seen == ["UUabc", "UULFabc", "UUSHabc", "UULVabc"]
        assert out == {"total": 2761, "long": 1759, "shorts": 942, "live": 60}
        # The invariant the whole approach rests on.
        assert out["long"] + out["shorts"] + out["live"] == out["total"]

    def test_404_means_the_channel_has_none_of_that_type(self):
        """A channel that never went live has no UULV playlist at all. That
        is a real zero, not a failure — persisting NULL there would lose
        information, and erroring would fail the whole channel."""
        import httpx

        client = _client()

        def fake_get(endpoint, params):
            if params["playlistId"].startswith("UULV"):
                raise httpx.HTTPStatusError(
                    "not found",
                    request=httpx.Request("GET", "http://x"),
                    response=httpx.Response(404),
                )
            return {"pageInfo": {"totalResults": 10}}

        with patch.object(client, "_get", side_effect=fake_get):
            out = client.get_channel_upload_breakdown("UCabc")

        assert out["live"] == 0
        assert out["live"] is not None

    def test_non_404_failure_reports_none_not_a_wrong_zero(self):
        """A 500 means we don't know. Recording 0 would silently claim the
        channel has no Shorts, which is a different and false statement."""
        import httpx

        client = _client()

        def fake_get(endpoint, params):
            if params["playlistId"].startswith("UUSH"):
                raise httpx.HTTPStatusError(
                    "boom",
                    request=httpx.Request("GET", "http://x"),
                    response=httpx.Response(500),
                )
            return {"pageInfo": {"totalResults": 10}}

        with patch.object(client, "_get", side_effect=fake_get):
            out = client.get_channel_upload_breakdown("UCabc")

        assert out["shorts"] is None


class TestGetChannelShortsIds:
    """Membership in UUSH is YouTube's own answer to "is this a Short?",
    which the duration heuristic gets wrong in both directions."""

    def test_collects_ids_across_pages(self):
        client = _client()
        page1 = {
            "items": [{"contentDetails": {"videoId": "a", "videoPublishedAt": "2026-03-01T00:00:00Z"}},
                      {"contentDetails": {"videoId": "b", "videoPublishedAt": "2026-02-01T00:00:00Z"}}],
            "nextPageToken": "t2",
        }
        page2 = {
            "items": [{"contentDetails": {"videoId": "c", "videoPublishedAt": "2026-01-01T00:00:00Z"}}],
        }
        with patch.object(client, "_get", side_effect=[page1, page2]):
            assert client.get_channel_shorts_ids("UCabc") == {"a", "b", "c"}

    def test_stops_once_past_the_sampled_window(self):
        """UUSH is newest-first and we only ever sampled a channel's recent
        uploads, so paginating a 4,000-Short channel to its end would burn
        quota for answers about videos we do not hold."""
        client = _client()
        page1 = {
            "items": [{"contentDetails": {"videoId": "a", "videoPublishedAt": "2026-03-01T00:00:00Z"}},
                      {"contentDetails": {"videoId": "old", "videoPublishedAt": "2020-01-01T00:00:00Z"}}],
            "nextPageToken": "t2",
        }
        calls = []

        def fake_get(endpoint, params):
            calls.append(params)
            return page1

        with patch.object(client, "_get", side_effect=fake_get):
            ids = client.get_channel_shorts_ids("UCabc", published_after="2026-01-01T00:00:00Z")

        assert len(calls) == 1, "should not have fetched a second page"
        assert "a" in ids

    def test_missing_shorts_playlist_yields_empty_set(self):
        import httpx

        client = _client()
        err = httpx.HTTPStatusError(
            "not found", request=httpx.Request("GET", "http://x"),
            response=httpx.Response(404),
        )
        with patch.object(client, "_get", side_effect=err):
            assert client.get_channel_shorts_ids("UCabc") == set()
