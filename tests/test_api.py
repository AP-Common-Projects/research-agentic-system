"""API-layer tests — fully mocked, no Postgres and no real subprocess launches.

Two behaviours get the most attention here because they are the ones that can
lie to an operator: a store failure must surface as an error rather than an
empty list, and run status must be derived from real telemetry rather than
assumed.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api import runs as runs_mod
from src.api import store_queries
from src.api.server import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def log_home(tmp_path, monkeypatch):
    """Point the run registry and log sink at a temp dir."""
    monkeypatch.setattr(runs_mod, "log_dir", lambda: tmp_path)
    return tmp_path


def _write_log(dirpath, run_id: str, entries: list[dict]) -> None:
    with (dirpath / f"{run_id}.jsonl").open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _write_registry(dirpath, entries: list[dict]) -> None:
    with (dirpath / "registry.jsonl").open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_reports_store_reachable(self, client):
        with patch("src.api.store_queries.ping", return_value=None):
            body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["store_reachable"] is True
        assert body["store_error"] is None

    def test_reports_store_unreachable_without_failing_request(self, client):
        """The console must still load when Postgres is down, showing a banner —
        so health returns 200 with the failure described, not a 5xx."""
        with patch(
            "src.api.store_queries.ping", side_effect=OSError("connection refused")
        ):
            resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["store_reachable"] is False
        assert "connection refused" in resp.json()["store_error"]

    def test_uses_the_fast_probe_not_a_real_query(self, client):
        """Regression: health used to call store_counts() (three aggregates,
        full pool timeout) — polled every 30s by the shell, that made a down
        store take up to the whole interval just to report itself down.
        Health must use the dedicated fast-fail probe instead."""
        with patch("src.api.store_queries.ping", return_value=None) as mock_ping, patch(
            "src.api.store_queries.store_counts"
        ) as mock_counts:
            client.get("/api/health")
        mock_ping.assert_called_once()
        mock_counts.assert_not_called()


class TestPing:
    def test_acquires_and_immediately_releases_a_connection(self):
        with patch("src.api.store_queries.get_connection") as mock_get, patch(
            "src.api.store_queries.put_connection"
        ) as mock_put:
            mock_conn = mock_get.return_value
            store_queries.ping(timeout=3.0)

        mock_get.assert_called_once_with(timeout=3.0)
        mock_put.assert_called_once_with(mock_conn)

    def test_propagates_a_pool_timeout_rather_than_swallowing_it(self):
        """ping() itself must not hide a failure — the caller (the /api/health
        route) is what's responsible for turning it into a banner."""
        with patch(
            "src.api.store_queries.get_connection", side_effect=TimeoutError("pool exhausted")
        ):
            with pytest.raises(TimeoutError):
                store_queries.ping()


# ---------------------------------------------------------------------------
# Run listing / status derivation
# ---------------------------------------------------------------------------


class TestListRuns:
    def test_status_running_when_process_alive(self, client, log_home):
        _write_registry(log_home, [{"run_id": "run-a", "thread_id": "t-a", "niches": ["x"], "pid": 999, "started_at": "2026-08-13T10:00:00Z"}])
        _write_log(log_home, "run-a", [{"node_name": "scan_niches", "cost_usd": 0.5, "timestamp": "2026-08-13T10:00:01Z"}])

        with patch.object(runs_mod, "is_process_running", return_value=True):
            runs = client.get("/api/runs").json()

        assert runs[0]["status"] == "running"
        assert runs[0]["last_node"] == "scan_niches"
        assert runs[0]["cost_usd"] == 0.5

    def test_status_complete_when_synthesize_logged(self, client, log_home):
        _write_registry(log_home, [{"run_id": "run-b", "thread_id": "t-b", "niches": [], "pid": 1, "started_at": "2026-08-13T10:00:00Z"}])
        _write_log(log_home, "run-b", [
            {"node_name": "scan_niches", "cost_usd": 0.1},
            {"node_name": "synthesize", "cost_usd": 0.2},
        ])

        with patch.object(runs_mod, "is_process_running", return_value=False):
            runs = client.get("/api/runs").json()

        assert runs[0]["status"] == "complete"

    def test_status_stopped_when_dead_without_synthesize(self, client, log_home):
        """A crashed run must not be reported as complete — that would hide
        the failure behind a green light."""
        _write_registry(log_home, [{"run_id": "run-c", "thread_id": "t-c", "niches": [], "pid": 1, "started_at": "2026-08-13T10:00:00Z"}])
        _write_log(log_home, "run-c", [{"node_name": "graph_walk", "cost_usd": 0.0}])

        with patch.object(runs_mod, "is_process_running", return_value=False):
            runs = client.get("/api/runs").json()

        assert runs[0]["status"] == "stopped"

    def test_endpoint_does_not_list_cli_started_runs(self, client, log_home):
        """Deliberate change of intent, not lost coverage.

        /api/runs used to fold in every run_id with a log file, which is how
        a CLI-started run became visible. It now backs the console's Live
        runs view, and the logs directory holds the lineage of the delivered
        workbooks -- ten runs behind finance.xlsx and crime.xlsx. Listing
        those presented finished provenance as things to monitor. They can't
        be deleted to clear the view either: the Spend page reads the same
        files to attribute cost per workbook.
        """
        _write_log(log_home, "run-cli", [{"node_name": "scan_niches", "thread_id": "t-cli"}])

        with patch.object(runs_mod, "is_process_running", return_value=False):
            runs = client.get("/api/runs").json()

        assert {r["run_id"] for r in runs} == set()

    def test_the_underlying_discovery_still_works_for_cost_totals(self, log_home):
        """The capability the endpoint stopped using is still there and still
        correct -- anything totalling historical spend depends on it."""
        _write_log(log_home, "run-cli", [{"node_name": "scan_niches", "thread_id": "t-cli"}])

        with patch.object(runs_mod, "is_process_running", return_value=False):
            runs = runs_mod.list_runs()

        row = next(r for r in runs if r["run_id"] == "run-cli")
        assert row["thread_id"] == "t-cli"


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------


class TestLaunchRun:
    def test_rejects_empty_niche_list(self, client):
        assert client.post("/api/runs", json={"niches": []}).status_code == 422

    def test_rejects_whitespace_only_niches(self, client):
        assert client.post("/api/runs", json={"niches": ["  ", ""]}).status_code == 422

    def test_launches_and_returns_ids(self, client):
        fake = {"run_id": "run-x", "thread_id": "t-x", "niches": ["3d printing"], "pid": 42}
        with patch.object(runs_mod, "launch_run", return_value=fake) as mock_launch:
            resp = client.post("/api/runs", json={"niches": ["3d printing"]})

        assert resp.status_code == 201
        assert resp.json()["run_id"] == "run-x"
        # depth is passed explicitly and defaults to None, so a request
        # without one still runs on the .env profile exactly as before.
        mock_launch.assert_called_once_with(["3d printing"], depth=None)


# ---------------------------------------------------------------------------
# Run detail
# ---------------------------------------------------------------------------


class TestGetRun:
    def test_unknown_run_is_404(self, client, log_home):
        assert client.get("/api/runs/nope").status_code == 404

    def test_checkpoint_failure_is_reported_not_swallowed(self, client, log_home):
        """A missing/unreachable checkpoint must be named in state_error — an
        empty state that looks like 'run has done nothing' is a lie."""
        _write_registry(log_home, [{"run_id": "run-d", "thread_id": "t-d", "niches": [], "pid": 1, "started_at": ""}])
        _write_log(log_home, "run-d", [{"node_name": "scan_niches"}])

        with patch.object(runs_mod, "is_process_running", return_value=False), patch(
            "src.api.server._load_checkpoint", side_effect=RuntimeError("no such thread")
        ):
            body = client.get("/api/runs/run-d").json()

        assert body["state"] is None
        assert "no such thread" in body["state_error"]

    def test_returns_checkpoint_state(self, client, log_home):
        _write_registry(log_home, [{"run_id": "run-e", "thread_id": "t-e", "niches": [], "pid": 1, "started_at": ""}])
        _write_log(log_home, "run-e", [{"node_name": "synthesize"}])

        state = {"selected_niche": "3d printing", "graph_walk_exclusive_count": 7}
        with patch.object(runs_mod, "is_process_running", return_value=False), patch(
            "src.api.server._load_checkpoint", return_value=state
        ):
            body = client.get("/api/runs/run-e").json()

        assert body["state"]["graph_walk_exclusive_count"] == 7
        assert body["state_error"] is None


class TestListReports:
    def test_only_runs_with_a_final_report_are_included(self, client, log_home):
        """A run that's still going, errored out, or saturated with nothing
        found must not show up as a broken/empty card."""
        _write_registry(log_home, [
            {"run_id": "run-with-report", "thread_id": "t-1", "niches": ["3d printing"], "pid": None, "started_at": "2026-08-13T10:00:00Z"},
            {"run_id": "run-no-report", "thread_id": "t-2", "niches": ["diy"], "pid": None, "started_at": "2026-08-13T09:00:00Z"},
        ])
        _write_log(log_home, "run-with-report", [{"node_name": "synthesize"}])
        _write_log(log_home, "run-no-report", [{"node_name": "graph_walk"}])

        report = {
            "niche": "3d printing",
            "generated_at": "2026-08-13T10:30:00Z",
            "summary": "Found a strong pattern.",
            "findings": [{"grade": "strong"}, {"grade": "strong"}, {"grade": "weak"}],
            "cannot_determine": ["causal claims excluded"],
            "discovery_stats": {"total_channels": 42},
        }

        def fake_checkpoint(thread_id):
            return {"final_report": report} if thread_id == "t-1" else {"final_report": None}

        with patch.object(runs_mod, "is_process_running", return_value=False), patch(
            "src.api.server._load_checkpoint", side_effect=fake_checkpoint
        ):
            body = client.get("/api/reports").json()

        assert len(body) == 1
        row = body[0]
        assert row["run_id"] == "run-with-report"
        assert row["niche"] == "3d printing"
        assert row["finding_count"] == 3
        assert row["grade_counts"] == {"strong": 2, "moderate": 0, "weak": 1}
        assert row["cannot_determine_count"] == 1
        assert row["discovery_stats"] == {"total_channels": 42}

    def test_checkpoint_failure_for_one_run_does_not_break_the_list(self, client, log_home):
        """Regression: a stale/unreachable checkpoint for one run must not
        take down the whole reports list — it should just be skipped."""
        _write_registry(log_home, [
            {"run_id": "run-ok", "thread_id": "t-ok", "niches": [], "pid": None, "started_at": "2026-08-13T10:00:00Z"},
            {"run_id": "run-broken", "thread_id": "t-broken", "niches": [], "pid": None, "started_at": "2026-08-13T09:00:00Z"},
        ])
        _write_log(log_home, "run-ok", [{"node_name": "synthesize"}])
        _write_log(log_home, "run-broken", [{"node_name": "synthesize"}])

        report = {"niche": "ok niche", "generated_at": "t", "summary": "", "findings": [], "cannot_determine": []}

        def fake_checkpoint(thread_id):
            if thread_id == "t-broken":
                raise RuntimeError("checkpoint gone")
            return {"final_report": report}

        with patch.object(runs_mod, "is_process_running", return_value=False), patch(
            "src.api.server._load_checkpoint", side_effect=fake_checkpoint
        ):
            body = client.get("/api/reports").json()

        assert len(body) == 1
        assert body[0]["run_id"] == "run-ok"

    def test_sorted_newest_first(self, client, log_home):
        _write_registry(log_home, [
            {"run_id": "run-older", "thread_id": "t-older", "niches": [], "pid": None, "started_at": ""},
            {"run_id": "run-newer", "thread_id": "t-newer", "niches": [], "pid": None, "started_at": ""},
        ])
        _write_log(log_home, "run-older", [{"node_name": "synthesize"}])
        _write_log(log_home, "run-newer", [{"node_name": "synthesize"}])

        def fake_checkpoint(thread_id):
            gen_at = "2026-08-13T08:00:00Z" if thread_id == "t-older" else "2026-08-13T12:00:00Z"
            return {"final_report": {"niche": "n", "generated_at": gen_at, "summary": "", "findings": [], "cannot_determine": []}}

        with patch.object(runs_mod, "is_process_running", return_value=False), patch(
            "src.api.server._load_checkpoint", side_effect=fake_checkpoint
        ):
            body = client.get("/api/reports").json()

        assert [r["run_id"] for r in body] == ["run-newer", "run-older"]

    def test_empty_when_no_reports_exist(self, client, log_home):
        assert client.get("/api/reports").json() == []


class TestRunLogs:
    def test_since_cursor_returns_only_new_entries(self, client, log_home):
        _write_log(log_home, "run-f", [
            {"node_name": "a"}, {"node_name": "b"}, {"node_name": "c"},
        ])
        body = client.get("/api/runs/run-f/logs", params={"since": 2}).json()
        assert [e["node_name"] for e in body["entries"]] == ["c"]
        assert body["cursor"] == 3

    def test_tolerates_partially_written_line(self, client, log_home):
        """The sink appends while the API reads; a torn final line must not
        take down the whole log view."""
        with (log_home / "run-g.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps({"node_name": "a"}) + "\n")
            f.write('{"node_name": "b"')  # truncated mid-write
        body = client.get("/api/runs/run-g/logs").json()
        assert [e["node_name"] for e in body["entries"]] == ["a"]


# ---------------------------------------------------------------------------
# Store endpoints
# ---------------------------------------------------------------------------


class TestStoreEndpoints:
    def test_store_failure_is_503_not_empty_list(self, client):
        with patch(
            "src.api.store_queries.search_channels", side_effect=OSError("pg down")
        ):
            resp = client.get("/api/store/channels")
        assert resp.status_code == 503
        assert "pg down" in resp.json()["detail"]

    def test_channels_passes_query_and_limit(self, client):
        with patch("src.api.store_queries.search_channels", return_value=[]) as mock_q:
            client.get("/api/store/channels", params={"q": "diy", "limit": 25})
        mock_q.assert_called_once_with("diy", 25)

    def test_limit_is_bounded(self, client):
        assert client.get("/api/store/channels", params={"limit": 9999}).status_code == 422

    def test_counts_includes_discovery_breakdown(self, client):
        with patch("src.api.store_queries.store_counts", return_value={"channels": 3, "videos": 9, "discovery_edges": 4}), patch(
            "src.api.store_queries.discovery_method_breakdown",
            return_value=[{"discovery_method": "graph_walk", "channel_count": 2}],
        ):
            body = client.get("/api/store/counts").json()
        assert body["channels"] == 3
        assert body["by_discovery_method"][0]["discovery_method"] == "graph_walk"

    def test_graph_returns_nodes_and_edges(self, client):
        payload = {"nodes": [{"channel_id": "UC1"}], "edges": []}
        with patch("src.api.store_queries.discovery_graph", return_value=payload):
            body = client.get("/api/store/graph").json()
        assert body["nodes"][0]["channel_id"] == "UC1"


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------


class TestCosts:
    def test_aggregates_by_node_and_run(self, client, log_home):
        _write_registry(log_home, [{"run_id": "run-h", "thread_id": "t-h", "niches": ["n"], "pid": 1, "started_at": ""}])
        _write_log(log_home, "run-h", [
            {"node_name": "build_taxonomy", "cost_usd": 0.10, "latency_ms": 200},
            {"node_name": "build_taxonomy", "cost_usd": 0.20, "latency_ms": 400},
            {"node_name": "graph_walk", "cost_usd": 0.0, "latency_ms": 50},
        ])

        with patch.object(runs_mod, "is_process_running", return_value=False):
            body = client.get("/api/costs").json()

        assert body["total_usd"] == pytest.approx(0.30)
        taxonomy = next(r for r in body["by_node"] if r["node_name"] == "build_taxonomy")
        assert taxonomy["calls"] == 2
        assert taxonomy["cost_usd"] == pytest.approx(0.30)
        assert taxonomy["avg_latency_ms"] == pytest.approx(300.0)
        assert body["by_run"][0]["run_id"] == "run-h"

    def test_unknown_api_route_404s_rather_than_returning_the_spa(self, client):
        """The SPA catch-all must not swallow /api/* typos — a client asking
        for a misspelled endpoint has to see a 404, not an HTML page."""
        resp = client.get("/api/definitely-not-a-route")
        assert resp.status_code == 404
        assert "text/html" not in resp.headers.get("content-type", "")

    def test_handles_missing_cost_and_latency_fields(self, client, log_home):
        """Nodes that emit NodeLog without cost/latency (the deterministic
        ones) must not break the cost view."""
        _write_registry(log_home, [{"run_id": "run-i", "thread_id": "t-i", "niches": [], "pid": 1, "started_at": ""}])
        _write_log(log_home, "run-i", [{"node_name": "select_next_node"}])

        with patch.object(runs_mod, "is_process_running", return_value=False):
            body = client.get("/api/costs").json()

        row = next(r for r in body["by_node"] if r["node_name"] == "select_next_node")
        assert row["cost_usd"] == 0.0
        assert row["avg_latency_ms"] is None
