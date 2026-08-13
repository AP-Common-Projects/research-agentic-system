"""Centralized configuration for the Omniframes YouTube Niche-Research Harness.

All tunable parameters live here — no magic numbers scattered through node code.
Environment variables provide secrets; this module provides typed, validated config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


class PostgresConfig(BaseSettings):
    host: str = "localhost"
    port: int = 5432
    database: str = "omniframes_harness"
    user: str = "omniframes"
    password: str = ""
    sslmode: str = "require"

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


class DeepSeekConfig(BaseSettings):
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"

    model_config = {"env_prefix": "DEEPSEEK_"}


class KimiConfig(BaseSettings):
    api_key: str = ""
    base_url: str = "https://api.moonshot.ai/v1"

    model_config = {"env_prefix": "KIMI_"}


class HarnessConfig(BaseSettings):
    budget_limit_usd: float = 10.0
    saturation_novelty_threshold: float = 0.05
    saturation_consecutive_window: int = 3
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    model_config = {"env_prefix": "", "extra": "allow"}


class Config:
    """Singleton config holder, populated from environment."""

    def __init__(self) -> None:
        self.postgres = PostgresConfig()
        self.youtube = YouTubeConfig()
        self.brightdata = BrightDataConfig()
        self.deepseek = DeepSeekConfig()
        self.kimi = KimiConfig()
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