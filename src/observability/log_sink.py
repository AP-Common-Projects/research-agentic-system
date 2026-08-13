"""JSON log sink for NodeLog records.

node_logs today only ever live inside HarnessState (in-memory / checkpointed
in Postgres) — there is no way to tail a run's progress without querying the
checkpointer. This sink writes each NodeLog to a per-run JSONL file the
moment a node produces it, independent of whether the run ultimately
succeeds, so a dashboard (or `tail -f`) can follow a run live.

One line per NodeLog, flushed immediately. Best-effort: a logging failure
must never fail the pipeline, so all I/O errors are swallowed here.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.config import get_config


def _log_path(run_id: str) -> Path:
    cfg = get_config()
    log_dir = Path(cfg.harness.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{run_id}.jsonl"


def write_node_logs(run_id: str, node_logs: list[dict]) -> None:
    if not run_id or not node_logs:
        return
    try:
        path = _log_path(run_id)
        with path.open("a", encoding="utf-8") as f:
            for entry in node_logs:
                f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass
