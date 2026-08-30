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


class TestGetChannelLongFormScan:
    """"Top 20 lifetime videos" cannot be answered from a recent-50 window —
    ranking the newest 50 uploads gives "best of the last few months", which
    for a 2,761-video channel is its most recent 1.8%."""

    def test_reads_the_long_form_playlist_not_all_uploads(self):
        """Scanning UULF rather than UU keeps Shorts and live VODs out of the
        quota budget entirely, and removes the need to post-filter."""
        client = _client()
        seen = []

        def fake_get(endpoint, params):
            if endpoint == "playlistItems":
                seen.append(params["playlistId"])
                return {"items": [{"contentDetails": {"videoId": "a"}}]}
            return {"items": []}

        with patch.object(client, "_get", side_effect=fake_get):
            client.get_channel_long_form_scan("UCabc")

        assert seen == ["UULFabc"]

    def test_paginates_past_the_first_page(self):
        client = _client()
        pages = [
            {"items": [{"contentDetails": {"videoId": "a"}}], "nextPageToken": "t2"},
            {"items": [{"contentDetails": {"videoId": "b"}}]},
        ]
        fetched = {}

        def fake_get(endpoint, params):
            if endpoint == "playlistItems":
                return pages.pop(0)
            fetched["ids"] = params["id"]
            return {"items": []}

        with patch.object(client, "_get", side_effect=fake_get):
            client.get_channel_long_form_scan("UCabc")

        assert fetched["ids"] == "a,b"

    def test_max_scan_bounds_a_pathological_channel(self):
        """One 15k-video channel must not consume a day's quota alone."""
        client = _client()
        page = {
            "items": [{"contentDetails": {"videoId": f"v{i}"}} for i in range(50)],
            "nextPageToken": "more",
        }
        fetched = {}

        def fake_get(endpoint, params):
            if endpoint == "playlistItems":
                return page
            fetched.setdefault("batches", []).append(params["id"].split(","))
            return {"items": []}

        with patch.object(client, "_get", side_effect=fake_get):
            client.get_channel_long_form_scan("UCabc", max_scan=60)

        assert sum(len(b) for b in fetched["batches"]) == 60


class TestGetChannelShortsSample:
    def test_reads_the_shorts_playlist_and_bounds_the_sample(self):
        client = _client()
        page = {
            "items": [{"contentDetails": {"videoId": f"s{i}"}} for i in range(50)],
            "nextPageToken": "more",
        }
        fetched = {}

        def fake_get(endpoint, params):
            if endpoint == "playlistItems":
                assert params["playlistId"] == "UUSHabc"
                return page
            fetched.setdefault("n", []).append(len(params["id"].split(",")))
            return {"items": []}

        with patch.object(client, "_get", side_effect=fake_get):
            client.get_channel_shorts_sample("UCabc", limit=10)

        assert sum(fetched["n"]) == 10

    def test_no_shorts_playlist_is_not_an_error(self):
        import httpx

        client = _client()
        err = httpx.HTTPStatusError(
            "not found", request=httpx.Request("GET", "http://x"),
            response=httpx.Response(404),
        )
        with patch.object(client, "_get", side_effect=err):
            assert client.get_channel_shorts_sample("UCabc") == []


class TestLongFormScanFallback:
    """UULF is only auto-generated for channels that have long-form uploads.
    A Shorts-only or very small channel 404s on it while still having a
    perfectly good uploads playlist — observed live at UU=41, UULF=404."""

    def test_falls_back_to_uploads_when_no_long_form_playlist(self):
        import httpx

        client = _client()
        seen = []

        def fake_get(endpoint, params):
            if endpoint == "playlistItems":
                pid = params["playlistId"]
                seen.append(pid)
                if pid.startswith("UULF"):
                    raise httpx.HTTPStatusError(
                        "not found", request=httpx.Request("GET", "http://x"),
                        response=httpx.Response(404),
                    )
                return {"items": [{"contentDetails": {"videoId": "a"}}]}
            return {"items": []}

        with patch.object(client, "_get", side_effect=fake_get):
            client.get_channel_long_form_scan("UCabc")

        assert seen == ["UULFabc", "UUabc"], "must retry against the uploads playlist"

    def test_gives_up_if_uploads_is_also_missing(self):
        """Both absent means the channel really has nothing fetchable —
        it must not loop retrying the same fallback."""
        import httpx

        client = _client()
        seen = []

        def fake_get(endpoint, params):
            seen.append(params["playlistId"])
            raise httpx.HTTPStatusError(
                "not found", request=httpx.Request("GET", "http://x"),
                response=httpx.Response(404),
            )

        with patch.object(client, "_get", side_effect=fake_get):
            assert client.get_channel_long_form_scan("UCabc") == []
        assert seen == ["UULFabc", "UUabc"]


class TestDeepScanGating:
    """~90% of discovered channels sit below the subscriber floor and can
    never reach the deliverable. Deep-scanning them exhausted the 10,000-unit
    daily quota in about an hour on the live augment runs and stalled
    discovery entirely."""

    def test_shallow_scan_reads_one_page_of_uploads(self):
        client = _client()
        seen = []

        def fake_get(endpoint, params):
            if endpoint == "playlistItems":
                seen.append(params["playlistId"])
                return {"items": [{"contentDetails": {"videoId": "a"}}],
                        "nextPageToken": "more"}
            return {"items": []}

        with patch.object(client, "_get", side_effect=fake_get):
            client.get_channel_videos("UCabc", max_results=50, deep_scan=False)

        assert seen == ["UUabc"], "one page of uploads, no UULF/UUSH walk"

    def test_deep_scan_walks_long_form_and_shorts(self):
        client = _client()
        seen = []

        def fake_get(endpoint, params):
            if endpoint == "playlistItems":
                seen.append(params["playlistId"][:4])
                return {"items": [{"contentDetails": {"videoId": "a"}}]}
            return {"items": []}

        with patch.object(client, "_get", side_effect=fake_get):
            client.get_channel_videos("UCabc", max_results=3000, deep_scan=True)

        assert "UULF" in seen and "UUSH" in seen

    def test_shallow_is_the_cheaper_path(self):
        client = _client()

        def counting(seen):
            def fake_get(endpoint, params):
                seen.append(endpoint)
                if endpoint == "playlistItems":
                    return {"items": [{"contentDetails": {"videoId": "a"}}]}
                return {"items": []}
            return fake_get

        shallow, deep = [], []
        with patch.object(client, "_get", side_effect=counting(shallow)):
            client.get_channel_videos("UCabc", max_results=50, deep_scan=False)
        with patch.object(_client(), "_get", side_effect=counting(deep)) as _:
            c2 = _client()
            with patch.object(c2, "_get", side_effect=counting(deep)):
                c2.get_channel_videos("UCabc", max_results=3000, deep_scan=True)
        assert len(shallow) < len(deep)


class TestApiKeyRotation:
    """The 10,000 units/day ceiling is per Google Cloud project. Keys from
    separate projects therefore carry separate allowances, and an exhausted
    one should hand over rather than end the run — an exhausted key has
    already killed a run mid-flight once, and produced 3,025 consecutive
    403s on another occasion."""

    def _client_with(self, keys):
        from src.tools.youtube_api import YouTubeAPIClient

        c = YouTubeAPIClient()
        c._api_keys = list(keys)
        c._key_index = 0
        c._quota_used = 0
        return c

    def _resp(self, status, body):
        import httpx

        return httpx.Response(
            status, json=body, request=httpx.Request("GET", "http://x")
        )

    def test_rotates_to_the_next_key_on_quota_exhaustion(self):
        import httpx

        c = self._client_with(["spent", "fresh"])
        used = []

        def fake(url, params=None, timeout=None):
            used.append(params["key"])
            if params["key"] == "spent":
                return self._resp(403, {"error": {"errors": [
                    {"reason": "quotaExceeded"}]}})
            return self._resp(200, {"items": [{"id": "x"}]})

        with patch("httpx.get", side_effect=fake):
            out = c._get("channels", {})

        assert used == ["spent", "fresh"]
        assert out == {"items": [{"id": "x"}]}

    def test_quota_counter_resets_so_the_new_allowance_is_usable(self):
        """Carrying the spent key's counter over would make check_quota
        refuse work the fresh key can happily do."""
        c = self._client_with(["spent", "fresh"])
        c._quota_used = 9_999
        assert c._rotate_key() is True
        assert c._quota_used == 0

    def test_other_403s_do_not_burn_a_key(self):
        """A disabled API or bad referrer fails identically on every key —
        rotating would just spend them all to reach the same error."""
        import httpx

        c = self._client_with(["one", "two"])

        def fake(url, params=None, timeout=None):
            return self._resp(403, {"error": {"errors": [
                {"reason": "accessNotConfigured"}]}})

        with patch("httpx.get", side_effect=fake):
            try:
                c._get("channels", {})
            except httpx.HTTPStatusError:
                pass
        assert c._key_index == 0, "must not rotate on a non-quota 403"

    def test_last_key_exhausted_raises_rather_than_looping(self):
        import httpx

        c = self._client_with(["only"])

        def fake(url, params=None, timeout=None):
            return self._resp(403, {"error": {"errors": [
                {"reason": "quotaExceeded"}]}})

        with patch("httpx.get", side_effect=fake):
            raised = False
            try:
                c._get("channels", {})
            except httpx.HTTPStatusError:
                raised = True
        assert raised


class TestApiKeysConfig:
    def test_primary_first_then_fallbacks_deduped(self):
        from src.config import YouTubeConfig

        cfg = YouTubeConfig(api_key="A", fallback_api_keys="B, C ,A,")
        assert cfg.api_keys == ["A", "B", "C"]

    def test_no_fallbacks_is_just_the_primary(self):
        """fallback_api_keys is passed explicitly: BaseSettings otherwise
        reads YOUTUBE_FALLBACK_API_KEYS from the developer's real .env, and
        the assertion would pass or fail depending on whose machine ran it."""
        from src.config import YouTubeConfig

        assert YouTubeConfig(api_key="A", fallback_api_keys="").api_keys == ["A"]
