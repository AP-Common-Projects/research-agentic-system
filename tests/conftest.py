"""Shared test fixtures.

The JSON log sink writes every NodeLog to ``<log_dir>/<run_id>.jsonl``. Tests
that drive `run_pipeline` would otherwise scatter fixture runs through the
repo's real ``logs/`` directory, where they are indistinguishable from actual
runs — and the console would list them as real history. Redirect the sink to a
temp directory for the whole suite.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_log_sink(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.observability.log_sink._log_path",
        lambda run_id: tmp_path / f"{run_id}.jsonl",
    )
    return tmp_path
