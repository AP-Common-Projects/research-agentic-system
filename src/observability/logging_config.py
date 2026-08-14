"""structlog configuration and the durable spend ledger.

Two gaps this closes, both flagged by the production audit.

**structlog was never configured.** Without a `configure()` call structlog
falls back to its console renderer on stdout — so `brightdata_triggered` and
`brightdata_collected`, the only place a `snapshot_id` ever appears, were not
JSON, not persisted, and gone when the terminal closed. Bright Data bills per
record and the snapshot id is the only handle for reconciling a charge against
the job that caused it.

**Spend was only ever recorded after the fact.** The trigger POST is the moment
money is committed; a lost response leaves a job billing with nothing anywhere
saying so. `record_spend_intent` writes to the durable JSONL sink *before* the
request goes out, so an interrupted run still leaves a trace of what it bought.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from src.config import get_config

_configured = False


def configure_logging() -> None:
    """JSON to stdout, at the configured level. Idempotent."""
    global _configured
    if _configured:
        return

    level = getattr(logging, get_config().harness.log_level, logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def _ledger_path(run_id: str) -> Path:
    """`logs/spend/<run_id>.jsonl` — a subdirectory, deliberately.

    The run listing globs `logs/*.jsonl` and treats every line as a NodeLog, so
    a ledger file sitting beside them is read as a malformed run and 500s the
    console. Keeping the two streams in separate directories means neither has
    to know about the other's shape.
    """
    cfg = get_config()
    ledger_dir = Path(cfg.harness.log_dir) / "spend"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    return ledger_dir / f"{run_id}.jsonl"


def record_spend_intent(
    run_id: str,
    collector: str,
    inputs: int,
    worst_case_records: int,
    params: dict[str, Any] | None = None,
    snapshot_id: str | None = None,
    phase: str = "trigger",
) -> None:
    """Append one line to the run's durable spend ledger.

    Called before the POST (`phase="trigger"`, no snapshot id yet) and again
    once the job is identified or collected. Best-effort: a ledger failure must
    never fail a run, which is also why it cannot be the *only* record — the
    NodeLog counts remain the authoritative total.
    """
    if not run_id:
        return
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "collector": collector,
            "inputs": inputs,
            "worst_case_records": worst_case_records,
            "snapshot_id": snapshot_id,
            "params": params or {},
        }
        with _ledger_path(run_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass


def total_records_spent(log_dir: Path | None = None) -> int:
    """Records billed across EVERY run the ledger has seen.

    The per-run `brightdata_record_budget` stops one run. It knows nothing
    about the account, so N runs each dutifully inside budget still add up to
    N x budget — which is how a 5,000-record allowance was spent without any
    single run misbehaving. This is the wallet-level number.

    Counts only `collected` events, whose `worst_case_records` holds the real
    returned count. Trigger-phase lines are intent, not spend, and counting
    them would double-bill every successful job.
    """
    base = log_dir or Path(get_config().harness.log_dir)
    total = 0
    for path in list((base / "spend").glob("*.jsonl")) + list(base.glob("*.spend.jsonl")):
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    if entry.get("phase") == "collected":
                        total += int(entry.get("worst_case_records") or 0)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return total


def read_spend_ledger(run_id: str) -> list[dict]:
    """Every spend event recorded for a run, for billing reconciliation."""
    path = _ledger_path(run_id)
    if not path.exists():
        return []
    entries: list[dict] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        pass
    return entries
