"""Centralized configuration for the YouTube Niche-Research Harness.

All tunable parameters live here — no magic numbers scattered through node code.
Environment variables provide secrets; this module provides typed, validated config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class PostgresConfig(BaseSettings):
    host: str = "localhost"
    port: int = 5432
    # `POSTGRES_DB` is the conventional name and what .env/.env.example set,
    # but env_prefix="POSTGRES_" would resolve this field as POSTGRES_DATABASE
    # — so without the alias the setting is silently ignored and the field
    # default wins. That default happens to match today, which is exactly what
    # makes it dangerous: point .env at another database and writes still go
    # to niche_harness.
    database: str = Field(default="niche_harness", validation_alias="POSTGRES_DB")
    user: str = "niche_harness"
    password: str = ""
    sslmode: str = "require"
    # psycopg_pool's own default is 30s. For an interactive console that's a
    # long time to sit on a spinner before the "store unreachable" banner
    # shows; for the harness's own writes, a healthy pool hands back a
    # connection in milliseconds, so this only matters when Postgres is
    # actually unreachable — where failing faster is strictly better.
    pool_timeout_seconds: float = 8.0

    model_config = {"env_prefix": "POSTGRES_"}

    @property
    def connection_string(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
            f"?sslmode={self.sslmode}"
        )

    @property
    def async_connection_string(self) -> str:
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
            f"?sslmode={self.sslmode}"
        )


class YouTubeConfig(BaseSettings):
    api_key: str = ""

    model_config = {"env_prefix": "YOUTUBE_"}


class BrightDataConfig(BaseSettings):
    """Bright Data Datasets API access.

    Three separate collectors, not one — `dataset_id` (singular) was a
    placeholder from before the real API surface was known and never worked.
    Each collector is triggered independently and billed per record.

    `mode` selects the live API or the recorded fixtures under `fixtures_dir`.
    Replay mode is what lets the whole graph be exercised end to end at zero
    record spend (see docs/first-run-plan.md, rung 03).
    """

    api_key: str = ""
    channels_dataset_id: str = "gd_lk538t2k2p1k3oos71"
    videos_dataset_id: str = "gd_lk56epmy2i5g7lzu0k"
    comments_dataset_id: str = "gd_lk9q0ew71spt1mxywf"

    mode: Literal["live", "replay"] = "live"
    fixtures_dir: str = "tests/fixtures/brightdata"

    # Discovery jobs run for minutes, not seconds — measured 2026-08-13, a
    # keyword discovery on the Channels collector took ~6min and one on the
    # Videos collector was still running after 12. by-URL collection is much
    # faster (~5s). The ceiling exists so a stuck snapshot fails the node
    # instead of hanging the run forever.
    poll_interval_seconds: float = 10.0
    poll_max_seconds: float = 900.0

    model_config = {"env_prefix": "BRIGHTDATA_"}


class OpenRouterConfig(BaseSettings):
    """DeepSeek and Kimi are both accessed through OpenRouter — one key, one
    endpoint, one billing/access path, instead of two direct vendor
    integrations (see ADR-0005)."""

    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"

    model_config = {"env_prefix": "OPENROUTER_"}


# Cost governors, preset per profile. Saturation stays the *intended* stop
# condition (master plan first principle #2) — these are the circuit breakers
# that keep a bug in the frontier logic from spending the month's record
# allowance before anyone notices. A run that stops on a governor rather than
# on saturation is a run to investigate, and check_saturation logs which it
# was so the two are never confused.
PROFILES: dict[str, dict[str, object]] = {
    "smoke": {
        "max_rounds_per_branch": 4,
        "max_tree_depth": 1,
        "max_branches": 1,
        "keyword_queries_per_round": 4,
        "keyword_results_per_query": 5,
        "graph_walk_frontier_per_round": 5,
        "min_subscribers_for_expansion": 500,
        "graph_walk_escalate_to_comments": False,
        "graph_walk_videos_per_channel": 2,
        "graph_walk_comments_per_video": 10,
        "brightdata_record_budget": 150,
        "youtube_quota_budget_per_run": 1000,
        "budget_limit_usd": 1.0,
    },
    "bounded": {
        "max_rounds_per_branch": 4,
        "max_tree_depth": 2,
        "max_branches": 4,
        "keyword_queries_per_round": 6,
        "keyword_results_per_query": 20,
        "graph_walk_frontier_per_round": 15,
        "min_subscribers_for_expansion": 1000,
        "graph_walk_escalate_to_comments": True,
        "graph_walk_videos_per_channel": 3,
        "graph_walk_comments_per_video": 10,
        "brightdata_record_budget": 1500,
        "youtube_quota_budget_per_run": 3000,
        "budget_limit_usd": 5.0,
    },
    "full": {
        "max_rounds_per_branch": 0,  # 0 == uncapped
        "max_tree_depth": 0,
        "max_branches": 0,
        "keyword_queries_per_round": 0,
        "keyword_results_per_query": 50,
        "graph_walk_frontier_per_round": 0,
        "min_subscribers_for_expansion": 0,
        "graph_walk_escalate_to_comments": True,
        "graph_walk_videos_per_channel": 5,
        "graph_walk_comments_per_video": 10,
        "brightdata_record_budget": 0,
        "youtube_quota_budget_per_run": 0,
        "budget_limit_usd": 10.0,
    },
}

DEFAULT_PROFILE = "bounded"


def profile_defaults(profile: str | None = None) -> dict[str, object]:
    """Preset values for a profile, minus anything the environment sets.

    Precedence is explicit env var > profile preset > field default. Filtering
    on os.environ here (rather than passing the whole preset as init kwargs) is
    what preserves that order — pydantic-settings ranks init kwargs *above*
    env vars, so an unfiltered preset would silently outrank a deliberate
    `GRAPH_WALK_FRONTIER_PER_ROUND=1` in .env.
    """
    name = (profile or os.getenv("HARNESS_PROFILE") or DEFAULT_PROFILE).lower()
    preset = PROFILES.get(name, PROFILES[DEFAULT_PROFILE])
    return {k: v for k, v in preset.items() if k.upper() not in os.environ}


class HarnessConfig(BaseSettings):
    profile: str = DEFAULT_PROFILE

    budget_limit_usd: float = 10.0
    saturation_novelty_threshold: float = 0.05
    saturation_consecutive_window: int = 3
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    youtube_daily_quota_ceiling: int = 10000
    youtube_quota_target_ratio: float = 0.90
    brightdata_max_concurrency: int = 10
    log_dir: str = "logs"

    # --- cost governors (0 means uncapped, for the `full` profile) ---
    max_rounds_per_branch: int = 3
    max_tree_depth: int = 2
    max_branches: int = 4
    keyword_queries_per_round: int = 6
    keyword_results_per_query: int = 20
    graph_walk_frontier_per_round: int = 15
    graph_walk_escalate_to_comments: bool = True
    graph_walk_videos_per_channel: int = 3
    graph_walk_comments_per_video: int = 10

    # Don't spend a record expanding a channel this small. Measured on 469
    # live discovery results: median subscriber count was 19, two thirds had
    # under 100 subs, and featured_channels (the free edge source) appears on
    # 1% of sub-100 channels versus 18% of 100k+ ones. Expanding the long tail
    # costs records and returns no edges. Discovered channels below this are
    # still hydrated and scored — they just aren't traversed from.
    min_subscribers_for_expansion: int = 1000
    brightdata_record_budget: int = 1500
    youtube_quota_budget_per_run: int = 3000

    # $1.50 per 1,000 records, Bright Data pay-as-you-go list rate.
    brightdata_cost_per_record_usd: float = 0.0015

    # Account-level ceiling, across ALL runs. brightdata_record_budget stops a
    # single run; nothing stopped the Nth run from spending the same budget
    # again, which is how a 5,000-record allowance went with no individual run
    # misbehaving. 0 disables the check. Set this to what the wallet actually
    # holds, not to what one run should cost.
    brightdata_account_record_budget: int = 0

    # Prompt-size caps. compact_branch and synthesize serialise store rows
    # straight into the prompt; without a cap a large branch produces a
    # six-figure-token request.
    max_prompt_channels: int = 100
    max_prompt_videos: int = 300

    # LangGraph's own default is 25, which a legitimately deep run can hit.
    # Set explicitly so the ceiling is a decision rather than an inheritance.
    graph_recursion_limit: int = 100

    model_config = {"env_prefix": "", "extra": "allow"}


class Config:
    """Singleton config holder, populated from environment."""

    def __init__(self, profile: str | None = None) -> None:
        self.postgres = PostgresConfig()
        self.youtube = YouTubeConfig()
        self.brightdata = BrightDataConfig()
        self.openrouter = OpenRouterConfig()
        resolved = (profile or os.getenv("HARNESS_PROFILE") or DEFAULT_PROFILE).lower()
        self.harness = HarnessConfig(
            profile=resolved, **profile_defaults(resolved)  # type: ignore[arg-type]
        )

    @classmethod
    def from_env(cls) -> Config:
        return cls()


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config