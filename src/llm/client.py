"""Unified LLM client supporting DeepSeek and Kimi via OpenAI-compatible APIs."""

from __future__ import annotations

import time
from typing import Any

import structlog
from openai import OpenAI
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.config import get_config

logger = structlog.get_logger(__name__)

MODEL_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": {"input": 0.44, "output": 0.87},
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28},
    "kimi-k3": {"input": 3.00, "output": 15.00},
    "kimi-k2.6": {"input": 0.95, "output": 4.00},
}

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _is_retryable(exception: BaseException) -> bool:
    from openai import (
        APIError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
    )

    if isinstance(exception, (RateLimitError, APITimeoutError, InternalServerError)):
        return True
    if isinstance(exception, APIError):
        return getattr(exception, "status_code", None) in RETRYABLE_STATUSES
    return False


def _create_retry_decorator():
    return retry(
        wait=wait_exponential_jitter(initial=1, max=60, jitter=2),
        stop=stop_after_attempt(5),
        retry=retry_if_exception(_is_retryable),
        before_sleep=lambda retry_state: logger.warning(
            "llm_retry",
            attempt=retry_state.attempt_number,
            exc_type=type(retry_state.outcome.exception()).__name__,
            wait_s=retry_state.next_action.sleep if retry_state.next_action else None,
        ),
    )


class LLMClient:
    def __init__(self) -> None:
        config = get_config()
        self._clients: dict[str, OpenAI] = {}
        self._config = config

    def _get_client(self, provider: str) -> OpenAI:
        if provider not in self._clients:
            if provider == "deepseek":
                self._clients[provider] = OpenAI(
                    api_key=self._config.deepseek.api_key,
                    base_url=self._config.deepseek.base_url,
                )
            elif provider == "kimi":
                self._clients[provider] = OpenAI(
                    api_key=self._config.kimi.api_key,
                    base_url=self._config.kimi.base_url,
                )
            else:
                raise ValueError(f"Unknown provider: {provider}")
        return self._clients[provider]

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        model: str = "deepseek-v4-pro",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        thinking: bool = False,
    ) -> dict[str, Any]:
        provider = (
            "deepseek"
            if model.startswith("deepseek")
            else "kimi"
            if model.startswith("kimi")
            else "deepseek"
        )
        client = self._get_client(provider)

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        start = time.monotonic()
        response = self._call_api(client, model, messages, temperature, max_tokens, thinking)
        latency_ms = (time.monotonic() - start) * 1000

        choice = response.choices[0]
        content = choice.message.content or ""
        prompt_tokens = response.usage.prompt_tokens if response.usage else 0
        completion_tokens = response.usage.completion_tokens if response.usage else 0
        cost = self._calculate_cost(model, prompt_tokens, completion_tokens)

        return {
            "content": content,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            "cost_usd": cost,
            "model": model,
            "latency_ms": latency_ms,
        }

    @_create_retry_decorator()
    def _call_api(
        self,
        client: OpenAI,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        thinking: bool,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if thinking and model.startswith("deepseek-v4-pro"):
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        return client.chat.completions.create(**kwargs)

    @staticmethod
    def _calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
        pricing = MODEL_PRICING.get(model)
        if pricing is None:
            return 0.0
        input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
        output_cost = (completion_tokens / 1_000_000) * pricing["output"]
        return round(input_cost + output_cost, 8)


_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client