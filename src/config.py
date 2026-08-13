"""Centralized configuration for the YouTube Niche-Research Harness.

All tunable parameters live here — no magic numbers scattered through node code.
Environment variables provide secrets; this module provides typed, validated config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class PostgresConfig(BaseSettings):
    host: str = "localhost"
    port: int = 5432
    database: str = "niche_harness"
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
    api_key: str = ""
    dataset_id: str = ""

    model_config = {"env_prefix": "BRIGHTDATA_"}


class OpenRouterConfig(BaseSettings):
    """DeepSeek and Kimi are both accessed through OpenRouter — one key, one
    endpoint, one billing/access path, instead of two direct vendor
    integrations (see ADR-0005)."""

    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"

    model_config = {"env_prefix": "OPENROUTER_"}


class HarnessConfig(BaseSettings):
    budget_limit_usd: float = 10.0
    saturation_novelty_threshold: float = 0.05
    saturation_consecutive_window: int = 3
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    youtube_daily_quota_ceiling: int = 10000
    youtube_quota_target_ratio: float = 0.90
    brightdata_max_concurrency: int = 10
    log_dir: str = "logs"

    model_config = {"env_prefix": "", "extra": "allow"}


class Config:
    """Singleton config holder, populated from environment."""

    def __init__(self) -> None:
        self.postgres = PostgresConfig()
        self.youtube = YouTubeConfig()
        self.brightdata = BrightDataConfig()
        self.openrouter = OpenRouterConfig()
        self.harness = HarnessConfig()

    @classmethod
    def from_env(cls) -> Config:
        return cls()


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config