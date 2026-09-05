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
    # Comma-separated fallback keys, each from a DIFFERENT Google Cloud
    # project. The 10,000 units/day ceiling is per project, not per key, so
    # a second key minted inside the same project shares the same exhausted
    # pool and buys nothing — verified the hard way when a same-project
    # swap changed nothing and a different-project key worked immediately.
    fallback_api_keys: str = ""

    @property
    def api_keys(self) -> list[str]:
        """Primary first, then fallbacks, blanks dropped."""
        keys = [self.api_key] + [
            k.strip() for k in self.fallback_api_keys.split(",")
        ]
        seen, out = set(), []
        for k in keys:
            if k and k not in seen:
                seen.add(k)
                out.append(k)
        return out

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
    # Separate from api_key on purpose. api_key authenticates
    # datasets/v3/trigger -- the collector calls that do actual discovery --
    # and needs a scope that permits collection; customer/balance needs
    # Admin or Finance. One Admin key can satisfy both (verified
    # 2026-09-05), but a Finance-scoped key only the second. Keeping them in
    # one field meant the console's Connect form wrote a balance token into
    # the field discovery authenticates with, betting the pipeline on the
    # pasted token's scope.
    billing_api_key: str = ""
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
        "min_channels_for_split": 5,
        "min_cluster_distinctness": 0.5,
        "cluster_seed": 42,
        "branch_lineage_budget_enabled": False,
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
        "min_channels_for_split": 8,
        "min_cluster_distinctness": 0.3,
        "cluster_seed": 42,
        "branch_lineage_budget_enabled": True,
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
        "min_channels_for_split": 3,
        "min_cluster_distinctness": 0.15,
        "cluster_seed": 42,
        "branch_lineage_budget_enabled": True,
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
    # Plateau detection: stop a track that has stopped IMPROVING, not just
    # one that has run out. Measured on Finance, keyword novelty settles at
    # ~0.65 and never nears the 0.05 threshold, so without this the keyword
    # track alone blocks saturation forever and every branch dies on a
    # governor. Reported as `novelty_plateaued`, never as exhaustion.
    plateau_detection_enabled: bool = True
    # Max round-on-round novelty change still counted as 'flat'. Observed
    # plateau deltas were +0.017, -0.05, 0.0.
    novelty_plateau_epsilon: float = 0.05
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    youtube_daily_quota_ceiling: int = 10000
    youtube_quota_target_ratio: float = 0.90
    brightdata_max_concurrency: int = 10
    log_dir: str = "logs"
    export_dir: str = "exports"
    # Top-N by outlier score. The full video table runs to five figures;
    # a Content Creation Team wants the actionable slice, not the dump.
    export_max_videos: int = 500
    # Measured on the Finance export: 706 hydrated channels + 1,824 distinct
    # unresolved frontier refs (seen via an edge, never fetched) = 2,530
    # nodes. When over this cap, unresolved nodes are trimmed first —
    # lowest-degree ones, since a frontier node with no other connection adds
    # the least to the picture — before any hydrated channel is dropped.
    export_max_graph_nodes: int = 2000

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

    # Wall-clock ceiling for one run, 0 == uncapped (the default, and what a
    # bare CLI invocation gets). Every other governor here bounds WORK, and
    # none of them bounds time: the console sells depth as a duration ("a
    # 30-minute run"), and on the first real launch a 30-minute tier was
    # tracking 3-5 hours. Records bought 285 channels, and classification
    # alone runs ~21 minutes per 50 channels, so that tier's own record
    # budget mandated ~2 hours of classification before discovery was even
    # counted. A duration label is only honest if something enforces it.
    #
    # Checked in check_saturation alongside the other run-level ceilings, so
    # hitting it exits the same way an exhausted budget does -- compact,
    # finalize, export what the run reached -- rather than aborting. That
    # also sets the granularity: the deadline is noticed between branch
    # cycles, not inside a node, so a long node in flight can overshoot it.
    run_deadline_seconds: int = 0

    # $1.50 per 1,000 records, Bright Data pay-as-you-go list rate.
    brightdata_cost_per_record_usd: float = 0.0015

    # Bright Data exposes a balance endpoint, but only to tokens carrying
    # billing permission; the collector tokens this harness uses get 403.
    # When that happens the console derives a balance from this figure minus
    # the record ledger, and labels the number "derived" rather than passing
    # an estimate off as a live reading. 0 means "no starting figure known",
    # and the console then shows the spend without a remaining balance.
    brightdata_starting_balance_usd: float = 0.0

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
    # Raised for v2: a multi-level graph-cluster-driven tree produces far more
    # supersteps than a flat 2-level tree. The actual guarantee is the
    # saturation and governor logic in check_saturation; this is the backstop.
    graph_recursion_limit: int = 200

    # --- adaptive depth (v2) ---
    # Communities smaller than this are dropped — below this threshold a
    # "cluster" can't be evidence-graded as corroborated.
    min_channels_for_split: int = 8
    # Intra/inter edge-weight ratio below which a community is algorithmic
    # noise, not real structure. Higher = stricter.
    min_cluster_distinctness: float = 0.3
    # Fixed seed for Louvain community detection. Without one, a resumed
    # run recomputes different clusters than the original — silent
    # nondeterminism wearing an idempotency PASS. Non-negotiable.
    cluster_seed: int = 42
    # Whether per-depth-1-lineage budget enforcement is active. Off in
    # smoke (the first live run shouldn't have a partially-implemented
    # governor as a new failure mode); on in bounded and full.
    branch_lineage_budget_enabled: bool = True

    # --- v3 dataset-first enrichment ---
    # Subscriber floor: channels below this skip expensive LLM/vision calls
    # unless a thriving override fires (§6.4). Base default from v3 plan.
    subscriber_floor: int = 50000
    # A single video with views >= N * channel's subscriber count triggers
    # the floor override — a breakout signal the sub count hasn't caught up to.
    breakout_video_multiplier: int = 10
    # Channels below the floor but with average views-per-video >= N * subs
    # pass the thriving test — the content is working faster than subs show.
    thriving_views_per_sub_multiplier: float = 5.0

    # Evergreen scoring (§6.1-6.2)
    # Threshold below which evergreen_score classifies a channel as news.
    news_evergreen_threshold: float = 30.0
    # Uploads per week above this threshold corroborates the news signal.
    news_high_frequency_threshold: float = 7.0

    # Engagement composite weights (§6.2). Must sum to 1.0 across the four
    # active components; hidden likes/comment axes get redistributed pro-rata.
    weight_views_per_sub: float = 0.40
    weight_comment_rate: float = 0.20
    weight_like_rate: float = 0.20
    weight_upload_consistency: float = 0.20

    # Priority-score weights for select_next_node's traversal order (§6.5)
    priority_weight_cluster_distinctness: float = 0.35
    priority_weight_evidence_richness: float = 0.25
    priority_weight_us_market: float = 0.25
    priority_weight_engagement_potential: float = 0.10
    priority_weight_depth_penalty: float = 0.05

    # Thumbnail-vision: how many recent thumbnails to sample per channel (§5.2)
    thumbnail_sample_count: int = 5

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