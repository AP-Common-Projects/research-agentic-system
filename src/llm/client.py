"""Unified LLM client — DeepSeek and Kimi models, accessed via OpenRouter.

Both model families go through one OpenAI-compatible endpoint and one API
key (ADR-0005), instead of two direct vendor integrations. Internal model
identifiers (below, and throughout state/cost-tracking/tests) are unchanged
by this — OPENROUTER_MODEL_SLUGS is the one place that translates them to
whatever OpenRouter's own catalog calls them for the actual API call.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from openai import OpenAI
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
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

# OpenRouter addresses models as "<upstream-provider>/<model-slug>". Verified
# 2026-08-13 against GET /api/v1/models on the live OpenRouter catalog — all
# four exist exactly as below. Nothing else in the codebase needs to change
# if these ever drift, since every other reference (pricing, cost tracking,
# state, tests) uses the internal name on the left.
OPENROUTER_MODEL_SLUGS: dict[str, str] = {
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "kimi-k3": "moonshotai/kimi-k3",
    "kimi-k2.6": "moonshotai/kimi-k2.6",
}

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class EmptyCompletionError(RuntimeError):
    """The provider answered, with nothing in it.

    Seen once on the technology run, where it cost a whole batch of thirty
    video descriptions: the node recorded "empty response" and moved on.
    It is a hiccup rather than a refusal -- the same prompt succeeds on the
    next attempt -- so it belongs with the other retryable conditions
    instead of being charged to the caller.
    """


def _is_retryable(exception: BaseException) -> bool:
    from openai import (
        APIError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
    )

    if isinstance(exception, EmptyCompletionError):
        return True
    if isinstance(exception, (RateLimitError, APITimeoutError, InternalServerError)):
        return True
    if isinstance(exception, APIError):
        return getattr(exception, "status_code", None) in RETRYABLE_STATUSES
    return False


#: Retries are bounded by wall clock as well as by count. An attempt count
#: alone assumes attempts are quick, and they are not: a mid-tier crime
#: batch that answers with nothing takes ~2.5 minutes to do it, so five
#: attempts spent twelve minutes on one batch of eight videos and pushed a
#: 30-minute heal budget to 40. Whichever limit is reached first wins.
_MAX_RETRY_SECONDS = 180


def _create_retry_decorator():
    return retry(
        wait=wait_exponential_jitter(initial=1, max=60, jitter=2),
        stop=(stop_after_attempt(5) | stop_after_delay(_MAX_RETRY_SECONDS)),
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
        self._config = get_config()
        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(
                api_key=self._config.openrouter.api_key,
                base_url=self._config.openrouter.base_url,
                default_headers={
                    "HTTP-Referer": "https://github.com/AP-Common-Projects/research-agentic-system",
                    "X-Title": "YouTube Niche-Research Harness",
                },
            )
        return self._client

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        model: str = "deepseek-v4-pro",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        thinking: bool = False,
    ) -> dict[str, Any]:
        client = self._get_client()

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        start = time.monotonic()
        response = self._call_api(client, model, messages, temperature, max_tokens, thinking)
        latency_ms = (time.monotonic() - start) * 1000

        choice = response.choices[0]
        content = choice.message.content or ""
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason not in (None, "stop"):
            # "length" means max_tokens cut the reply off; the JSON repair
            # downstream can close the brackets but the lost items are gone.
            logger.warning(
                "llm_completion_incomplete",
                model=model, finish_reason=finish_reason, chars=len(content),
            )
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
            "finish_reason": finish_reason,
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
            "model": OPENROUTER_MODEL_SLUGS.get(model, model),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if thinking and model.startswith("deepseek-v4-pro"):
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        response = client.chat.completions.create(**kwargs)
        self._raise_if_blank(response, model)
        return response

    @staticmethod
    def _raise_if_blank(response: Any, model: str) -> None:
        """Reject a completion with nothing in it.

        Called inside the retried request rather than by the caller: every
        prompt this client sends asks for content back, so a blank reply is
        a failed call and earns the same backoff as a 503. A model that
        answers "I cannot help with that" has answered, and is left alone.
        """
        choice = response.choices[0] if response.choices else None
        content = getattr(getattr(choice, "message", None), "content", None) or ""
        if content.strip():
            return
        raise EmptyCompletionError(
            f"{model} returned no content "
            f"(finish_reason={getattr(choice, 'finish_reason', None)})"
        )

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