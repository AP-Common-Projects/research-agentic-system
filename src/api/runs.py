"""Run registry and launch control for the web API.

A run is launched as a detached `python -m src.cli` subprocess — the same
entrypoint an engineer would type by hand — so the API adds an interface,
never a second execution path. Three artefacts identify a run afterwards:

- ``logs/registry.jsonl`` — run_id → thread_id/niches/pid, written at launch.
- ``logs/<run_id>.jsonl``  — the NodeLog stream from ``src.observability``.
- the Postgres checkpoint keyed by thread_id — authoritative graph state.

The registry exists because the log sink keys files by ``run_id`` while the
checkpointer keys state by ``thread_id``; without it the API could not join
a run's telemetry to its graph state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.config import get_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def log_dir() -> Path:
    """Resolve the configured log directory relative to the repo root."""
    raw = Path(get_config().harness.log_dir)
    return raw if raw.is_absolute() else REPO_ROOT / raw


def registry_path() -> Path:
    return log_dir() / "registry.jsonl"


def run_log_path(run_id: str) -> Path:
    return log_dir() / f"{run_id}.jsonl"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def load_registry() -> list[dict[str, Any]]:
    path = registry_path()
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _append_registry(entry: dict[str, Any]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _is_zombie(pid: int) -> bool:
    """True if `pid` has exited but has not been reaped.

    launch_run starts each run with Popen and discards the handle, so nothing
    ever wait()s on it. A finished child therefore stays in the process table
    as a zombie for as long as the server lives -- and os.kill(pid, 0)
    SUCCEEDS on a zombie, because the entry still exists. Existence alone is
    not liveness.
    """
    try:
        # `comm` is parenthesised and may itself contain spaces or ')', so
        # the state field is the first token after the LAST ')'.
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 1:].split()[0] == "Z"
    except (OSError, ValueError, IndexError):
        # Not Linux, or the entry vanished between calls. Either way this is
        # not evidence of a zombie, and the caller's os.kill result stands.
        return False


def is_process_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False

    if _is_zombie(pid):
        # Reap it while we are here, so the entry does not sit in the table
        # until the server exits. Only works if this process is the parent,
        # which it is for anything launch_run started; a run inherited from a
        # previous server is simply reported as finished, which it is.
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        return False

    return True


def launch_run(niches: list[str], depth: str | None = None) -> dict[str, Any]:
    """Start a detached harness run and register it. Returns the registry entry.

    Budget remains a circuit breaker rather than a per-run dial (master plan
    §1): .env still sets the account-level ceiling and nothing here can raise
    it. `depth` selects a named tier from src/api/depth.py, which sets the
    same governors PROFILES already define -- rounds, tree depth, branches,
    query breadth, record and quota budgets -- for this one run, passed as
    environment to the subprocess so the child resolves them through the
    normal config path instead of a second one.

    A run launched without a depth behaves exactly as before, on the profile
    from .env.
    """
    if not niches:
        raise ValueError("At least one candidate niche is required.")

    env = os.environ.copy()
    tier = None
    if depth:
        from src.api.depth import get_tier

        tier = get_tier(depth)
        if tier is None:
            raise ValueError(f"Unknown depth tier: {depth!r}")
        for key, value in tier.governors.items():
            env[key] = str(value)

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    thread_id = f"thread-{uuid.uuid4().hex[:12]}"

    log_dir().mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir() / f"{run_id}.stdout.log"

    with stdout_path.open("w", encoding="utf-8") as stdout_f:
        proc = subprocess.Popen(
            # --run-id/--thread-id pin the child to the ids just minted above,
            # not ones of its own -- cli.py otherwise mints its own on every
            # invocation, which left every console-launched run's registry
            # entry pointing at a run_id and thread_id the child never wrote
            # a single log line under. The child's own NodeLog, checkpoint
            # and export all land under these ids as a result; only the
            # in-process log file naming and the LangGraph thread ever read
            # them, so this changes nothing for a plain CLI invocation.
            [
                sys.executable, "-m", "src.cli", *niches, "--json",
                "--run-id", run_id, "--thread-id", thread_id,
            ],
            cwd=str(REPO_ROOT),
            stdout=stdout_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

    entry = {
        "run_id": run_id,
        "thread_id": thread_id,
        "niches": niches,
        "pid": proc.pid,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "depth": tier.id if tier else None,
        "depth_label": tier.label if tier else None,
        "depth_hours": tier.hours if tier else None,
    }
    _append_registry(entry)
    return entry


# ---------------------------------------------------------------------------
# NodeLog stream
# ---------------------------------------------------------------------------


def read_run_log(run_id: str, since: int = 0) -> list[dict[str, Any]]:
    """Read NodeLog entries for a run, skipping the first ``since`` lines."""
    path = run_log_path(run_id)
    if not path.exists():
        return []

    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for index, line in enumerate(f):
            if index < since:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def iter_new_log_lines(run_id: str, since: int = 0) -> Iterator[dict[str, Any]]:
    """Yield NodeLog entries appended after ``since``, then stop.

    The SSE endpoint calls this repeatedly rather than holding an open file
    handle, so a run that rotates or recreates its log is picked up.
    """
    yield from read_run_log(run_id, since=since)


def known_run_ids() -> list[str]:
    """Every run_id with a log file, newest first by mtime."""
    d = log_dir()
    if not d.exists():
        return []
    # Only NodeLog streams. `registry` is the launch index, and `*.spend.jsonl`
    # is the Bright Data spend ledger — a different shape entirely, which the
    # loop below would read as a malformed run.
    files = [
        p for p in d.glob("*.jsonl")
        if p.stem != "registry" and not p.stem.endswith(".spend")
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.stem for p in files]


def list_runs() -> list[dict[str, Any]]:
    """Merge the registry with any log-only runs (started from the CLI)."""
    registry = {e["run_id"]: dict(e) for e in load_registry()}

    for run_id in known_run_ids():
        if run_id not in registry:
            entries = read_run_log(run_id)
            registry[run_id] = {
                "run_id": run_id,
                "thread_id": entries[0].get("thread_id", "") if entries else "",
                "niches": [],
                "pid": None,
                "started_at": entries[0].get("timestamp", "") if entries else "",
            }

    runs: list[dict[str, Any]] = []
    for run_id, entry in registry.items():
        entries = read_run_log(run_id)
        running = is_process_running(entry.get("pid"))
        has_report = any(e.get("node_name") == "synthesize" for e in entries)

        if running:
            status = "running"
        elif has_report:
            status = "complete"
        elif entries:
            status = "stopped"
        else:
            status = "pending"

        runs.append(
            {
                **entry,
                "status": status,
                # .get, not [...]: one unexpected line shape must not 500 the
                # whole run listing. A sibling .jsonl written by another
                # subsystem once did exactly that.
                "last_node": entries[-1].get("node_name") if entries else None,
                "last_activity_at": entries[-1].get("timestamp") if entries else None,
                "log_lines": len(entries),
                "cost_usd": round(sum(float(e.get("cost_usd") or 0.0) for e in entries), 6),
            }
        )

    runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return runs


def get_run(run_id: str) -> dict[str, Any] | None:
    for run in list_runs():
        if run["run_id"] == run_id:
            return run
    return None
