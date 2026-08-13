"""Cascade/tier router — maps logical tiers to concrete model+provider combos.

Production tiers (frontier/mid/cheap) default to the DeepSeek lineage and
fall back to the Kimi family on rate-limit/timeout. cross_judge and
thumbnail_vision have no fallback: cross_judge must stay a different family
than production (a model can't grade its own output), and thumbnail_vision
needs Kimi's native vision (DeepSeek V4 is text-only).
"""

from __future__ import annotations

from typing import Any, TypedDict

import structlog

from src.llm.client import get_client

logger = structlog.get_logger(__name__)


class TierConfig(TypedDict):
    provider: str
    model_name: str
    thinking: bool
    temperature: float
    max_tokens: int
    input_cost_per_1m: float
    output_cost_per_1m: float


TIER_MAP: dict[str, TierConfig] = {
    "frontier": {
        "provider": "deepseek",
        "model_name": "deepseek-v4-pro",
        "thinking": True,
        "temperature": 0.0,
        "max_tokens": 16384,
        "input_cost_per_1m": 0.44,
        "output_cost_per_1m": 0.87,
    },
    "mid": {
        "provider": "deepseek",
        "model_name": "deepseek-v4-pro",
        "thinking": False,
        "temperature": 0.0,
        "max_tokens": 8192,
        "input_cost_per_1m": 0.44,
        "output_cost_per_1m": 0.87,
    },
    "cheap": {
        "provider": "deepseek",
        "model_name": "deepseek-v4-flash",
        "thinking": False,
        "temperature": 0.7,
        "max_tokens": 4096,
        "input_cost_per_1m": 0.14,
        "output_cost_per_1m": 0.28,
    },
    "cross_judge": {
        "provider": "kimi",
        "model_name": "kimi-k3",
        "thinking": False,
        "temperature": 0.0,
        "max_tokens": 4096,
        "input_cost_per_1m": 3.00,
        "output_cost_per_1m": 15.00,
    },
    "thumbnail_vision": {
        "provider": "kimi",
        "model_name": "kimi-k2.6",
        "thinking": False,
        "temperature": 0.0,
        "max_tokens": 4096,
        "input_cost_per_1m": 0.95,
        "output_cost_per_1m": 4.00,
    },
}

FALLBACK_MAP: dict[str, TierConfig] = {
    "frontier": {
        "provider": "kimi",
        "model_name": "kimi-k3",
        "thinking": False,
        "temperature": 0.0,
        "max_tokens": 16384,
        "input_cost_per_1m": 3.00,
        "output_cost_per_1m": 15.00,
    },
    "mid": {
        "provider": "kimi",
        "model_name": "kimi-k3",
        "thinking": False,
        "temperature": 0.0,
        "max_tokens": 8192,
        "input_cost_per_1m": 3.00,
        "output_cost_per_1m": 15.00,
    },
    "cheap": {
        "provider": "kimi",
        "model_name": "kimi-k2.6",
        "thinking": False,
        "temperature": 0.7,
        "max_tokens": 4096,
        "input_cost_per_1m": 0.95,
        "output_cost_per_1m": 4.00,
    },
}


def _is_fallbackable(exc: BaseException) -> bool:
    try:
        from openai import APITimeoutError, RateLimitError

        return isinstance(exc, (RateLimitError, APITimeoutError))
    except ImportError:
        return False


def get_model_for_tier(tier: str) -> TierConfig:
    cfg = TIER_MAP.get(tier)
    if cfg is None:
        raise ValueError(
            f"Unknown tier: {tier}. Valid tiers: {sorted(TIER_MAP.keys())}"
        )
    return cfg


def _complete(cfg: TierConfig, prompt: str, system: str | None) -> dict[str, Any]:
    client = get_client()
    return client.complete(
        prompt=prompt,
        system=system,
        model=cfg["model_name"],
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
        thinking=cfg["thinking"],
    )


def complete_tier(
    tier: str,
    prompt: str,
    system: str | None = None,
) -> dict[str, Any]:
    cfg = get_model_for_tier(tier)
    try:
        return _complete(cfg, prompt, system)
    except Exception as exc:
        fallback = FALLBACK_MAP.get(tier)
        if fallback is None or not _is_fallbackable(exc):
            raise
        logger.warning(
            "llm_family_fallback",
            tier=tier,
            from_model=cfg["model_name"],
            to_model=fallback["model_name"],
            exc_type=type(exc).__name__,
        )
        result = _complete(fallback, prompt, system)
        result["fallback_used"] = True
        return result


def estimate_cost(
    tier: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    cfg = get_model_for_tier(tier)
    input_cost = (prompt_tokens / 1_000_000) * cfg["input_cost_per_1m"]
    output_cost = (completion_tokens / 1_000_000) * cfg["output_cost_per_1m"]
    return round(input_cost + output_cost, 8)