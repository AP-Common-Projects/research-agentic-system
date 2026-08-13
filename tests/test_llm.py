"""Tests for src/llm — client, cascade routing, pricing.

Zero real API calls. All openai calls are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.completion_usage import CompletionUsage

from src.llm.cascade import complete_tier, estimate_cost, get_model_for_tier
from src.llm.client import LLMClient, get_client


def _fake_completion(content: str, prompt_tokens: int = 100, completion_tokens: int = 50) -> ChatCompletion:
    return ChatCompletion(
        id="fake-id",
        created=1234567890,
        model="fake-model",
        object="chat.completion",
        choices=[
            Choice(
                finish_reason="stop",
                index=0,
                message=ChatCompletionMessage(content=content, role="assistant"),
            )
        ],
        usage=CompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------

class TestLLMClientComplete:
    @patch("src.llm.client.get_config")
    def test_returns_expected_structure(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = _fake_completion("Hello, world!")

        with patch.object(client._get_client("deepseek").chat.completions, "create", return_value=fake):
            result = client.complete(
                prompt="Say hello",
                system="You are helpful.",
                model="deepseek-v4-pro",
                temperature=0.0,
                max_tokens=4096,
                thinking=True,
            )

        assert result["content"] == "Hello, world!"
        assert result["usage"]["prompt_tokens"] == 100
        assert result["usage"]["completion_tokens"] == 50
        assert result["model"] == "deepseek-v4-pro"
        assert result["cost_usd"] > 0
        assert isinstance(result["latency_ms"], float)
        assert result["latency_ms"] >= 0

    @patch("src.llm.client.get_config")
    def test_handles_none_content(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = ChatCompletion(
            id="f",
            created=1,
            model="m",
            object="chat.completion",
            choices=[
                Choice(
                    finish_reason="stop",
                    index=0,
                    message=ChatCompletionMessage(content=None, role="assistant"),
                )
            ],
            usage=CompletionUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        with patch.object(client._get_client("deepseek").chat.completions, "create", return_value=fake):
            result = client.complete(prompt="test", model="deepseek-v4-pro")

        assert result["content"] == ""

    @patch("src.llm.client.get_config")
    def test_handles_no_usage(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = ChatCompletion(
            id="f",
            created=1,
            model="m",
            object="chat.completion",
            choices=[
                Choice(
                    finish_reason="stop",
                    index=0,
                    message=ChatCompletionMessage(content="ok", role="assistant"),
                )
            ],
            usage=None,
        )

        with patch.object(client._get_client("deepseek").chat.completions, "create", return_value=fake):
            result = client.complete(prompt="test", model="deepseek-v4-pro")

        assert result["usage"]["prompt_tokens"] == 0
        assert result["usage"]["completion_tokens"] == 0
        assert result["cost_usd"] == 0.0

    @patch("src.llm.client.get_config")
    def test_pricing_deepseek_v4_pro(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = _fake_completion("ok", prompt_tokens=1_000_000, completion_tokens=1_000_000)

        with patch.object(client._get_client("deepseek").chat.completions, "create", return_value=fake):
            result = client.complete(prompt="test", model="deepseek-v4-pro")

        assert result["cost_usd"] == pytest.approx(0.44 + 0.87)

    @patch("src.llm.client.get_config")
    def test_pricing_deepseek_v4_flash(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = _fake_completion("ok", prompt_tokens=1_000_000, completion_tokens=1_000_000)

        with patch.object(client._get_client("deepseek").chat.completions, "create", return_value=fake):
            result = client.complete(prompt="test", model="deepseek-v4-flash")

        assert result["cost_usd"] == pytest.approx(0.14 + 0.28)

    @patch("src.llm.client.get_config")
    def test_pricing_kimi_k3(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = _fake_completion("ok", prompt_tokens=1_000_000, completion_tokens=1_000_000)

        with patch.object(client._get_client("kimi").chat.completions, "create", return_value=fake):
            result = client.complete(prompt="test", model="kimi-k3")

        assert result["cost_usd"] == pytest.approx(3.00 + 15.00)

    @patch("src.llm.client.get_config")
    def test_pricing_kimi_k2_6(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = _fake_completion("ok", prompt_tokens=1_000_000, completion_tokens=1_000_000)

        with patch.object(client._get_client("kimi").chat.completions, "create", return_value=fake):
            result = client.complete(prompt="test", model="kimi-k2.6")

        assert result["cost_usd"] == pytest.approx(0.95 + 4.00)

    @patch("src.llm.client.get_config")
    def test_unknown_model_zero_cost(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = _fake_completion("ok", prompt_tokens=1000, completion_tokens=500)

        with patch.object(client._get_client("deepseek").chat.completions, "create", return_value=fake):
            result = client.complete(prompt="test", model="unknown-model")

        assert result["cost_usd"] == 0.0

    @patch("src.llm.client.get_config")
    def test_system_prompt_included(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = _fake_completion("ok")
        mock_create = MagicMock(return_value=fake)
        client._get_client("deepseek").chat.completions.create = mock_create

        client.complete(
            prompt="user message",
            system="system message",
            model="deepseek-v4-pro",
        )

        called_messages = mock_create.call_args.kwargs["messages"]
        assert called_messages[0] == {"role": "system", "content": "system message"}
        assert called_messages[1] == {"role": "user", "content": "user message"}

    @patch("src.llm.client.get_config")
    def test_no_system_prompt(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = _fake_completion("ok")
        mock_create = MagicMock(return_value=fake)
        client._get_client("deepseek").chat.completions.create = mock_create

        client.complete(prompt="user message", model="deepseek-v4-pro")

        called_messages = mock_create.call_args.kwargs["messages"]
        assert len(called_messages) == 1
        assert called_messages[0]["role"] == "user"

    @patch("src.llm.client.get_config")
    def test_thinking_enabled_for_deepseek_v4_pro(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = _fake_completion("ok")
        mock_create = MagicMock(return_value=fake)
        client._get_client("deepseek").chat.completions.create = mock_create

        client.complete(prompt="test", model="deepseek-v4-pro", thinking=True)

        assert mock_create.call_args.kwargs.get("extra_body") == {"thinking": {"type": "enabled"}}

    @patch("src.llm.client.get_config")
    def test_thinking_not_sent_for_cheap_model(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = _fake_completion("ok")
        mock_create = MagicMock(return_value=fake)
        client._get_client("deepseek").chat.completions.create = mock_create

        client.complete(prompt="test", model="deepseek-v4-flash", thinking=True)

        assert "extra_body" not in mock_create.call_args.kwargs

    @patch("src.llm.client.get_config")
    def test_provider_routing_deepseek(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-ds"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-kimi"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        ds_key = client._get_client("deepseek").api_key
        assert ds_key == "sk-ds"

    @patch("src.llm.client.get_config")
    def test_provider_routing_kimi(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-ds"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-kimi"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        kimi_key = client._get_client("kimi").api_key
        assert kimi_key == "sk-kimi"

    @patch("src.llm.client.get_config")
    def test_unknown_provider_raises(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        with pytest.raises(ValueError, match="Unknown provider"):
            client._get_client("openai")


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

class TestRetryDecisions:
    @patch("src.llm.client.get_config")
    def test_rate_limit_is_retryable(self, mock_get_config):
        from openai import RateLimitError
        from src.llm.client import _is_retryable

        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        exc = RateLimitError("too many", response=MagicMock(), body=None)
        assert _is_retryable(exc) is True

    @patch("src.llm.client.get_config")
    def test_timeout_is_retryable(self, mock_get_config):
        from openai import APITimeoutError
        from src.llm.client import _is_retryable

        exc = APITimeoutError("timeout")
        assert _is_retryable(exc) is True

    @patch("src.llm.client.get_config")
    def test_server_error_is_retryable(self, mock_get_config):
        import httpx
        from openai import InternalServerError
        from src.llm.client import _is_retryable

        resp = httpx.Response(500, request=httpx.Request("GET", "http://x"))
        exc = InternalServerError("server error", response=resp, body=None)
        assert _is_retryable(exc) is True

    @patch("src.llm.client.get_config")
    def test_bad_request_is_not_retryable(self, mock_get_config):
        import httpx
        from openai import BadRequestError
        from src.llm.client import _is_retryable

        resp = httpx.Response(400, request=httpx.Request("GET", "http://x"))
        exc = BadRequestError("bad request", response=resp, body=None)
        assert _is_retryable(exc) is False

    @patch("src.llm.client.get_config")
    def test_attribute_error_is_not_retryable(self, mock_get_config):
        from src.llm.client import _is_retryable

        assert _is_retryable(ValueError("not an API error")) is False


# ---------------------------------------------------------------------------
# Cascade routing
# ---------------------------------------------------------------------------

class TestGetModelForTier:
    def test_frontier_tier(self):
        cfg = get_model_for_tier("frontier")
        assert cfg["provider"] == "deepseek"
        assert cfg["model_name"] == "deepseek-v4-pro"
        assert cfg["thinking"] is True
        assert cfg["temperature"] == 0.0

    def test_mid_tier(self):
        cfg = get_model_for_tier("mid")
        assert cfg["provider"] == "deepseek"
        assert cfg["model_name"] == "deepseek-v4-pro"
        assert cfg["thinking"] is False

    def test_cheap_tier(self):
        cfg = get_model_for_tier("cheap")
        assert cfg["provider"] == "deepseek"
        assert cfg["model_name"] == "deepseek-v4-flash"
        assert cfg["thinking"] is False
        assert cfg["temperature"] == 0.7

    def test_cross_judge_tier(self):
        cfg = get_model_for_tier("cross_judge")
        assert cfg["provider"] == "kimi"
        assert cfg["model_name"] == "kimi-k3"
        assert cfg["thinking"] is False
        assert cfg["temperature"] == 0.0

    def test_thumbnail_vision_tier(self):
        cfg = get_model_for_tier("thumbnail_vision")
        assert cfg["provider"] == "kimi"
        assert cfg["model_name"] == "kimi-k2.6"
        assert cfg["thinking"] is False
        assert cfg["temperature"] == 0.0

    def test_unknown_tier_raises(self):
        with pytest.raises(ValueError, match="Unknown tier"):
            get_model_for_tier("nonexistent")

    def test_all_tiers_have_required_keys(self):
        required = {"provider", "model_name", "thinking", "temperature", "max_tokens", "input_cost_per_1m", "output_cost_per_1m"}
        for tier in ["frontier", "mid", "cheap", "cross_judge", "thumbnail_vision"]:
            cfg = get_model_for_tier(tier)
            missing = required - set(cfg.keys())
            assert not missing, f"Tier {tier} missing keys: {missing}"


class TestCompleteTier:
    @patch("src.llm.cascade.get_client")
    @patch("src.llm.cascade.get_model_for_tier")
    def test_routes_to_frontier(self, mock_get_tier, mock_get_client):
        mock_get_tier.return_value = {
            "provider": "deepseek",
            "model_name": "deepseek-v4-pro",
            "thinking": True,
            "temperature": 0.0,
            "max_tokens": 16384,
            "input_cost_per_1m": 0.44,
            "output_cost_per_1m": 0.87,
        }
        mock_client = MagicMock()
        mock_client.complete.return_value = {"content": "result", "cost_usd": 0.01}
        mock_get_client.return_value = mock_client

        result = complete_tier("frontier", "test prompt", "system msg")

        mock_client.complete.assert_called_once_with(
            prompt="test prompt",
            system="system msg",
            model="deepseek-v4-pro",
            temperature=0.0,
            max_tokens=16384,
            thinking=True,
        )
        assert result == {"content": "result", "cost_usd": 0.01}

    @patch("src.llm.cascade.get_client")
    @patch("src.llm.cascade.get_model_for_tier")
    def test_routes_to_cheap_without_system(self, mock_get_tier, mock_get_client):
        mock_get_tier.return_value = {
            "provider": "deepseek",
            "model_name": "deepseek-v4-flash",
            "thinking": False,
            "temperature": 0.7,
            "max_tokens": 4096,
            "input_cost_per_1m": 0.14,
            "output_cost_per_1m": 0.28,
        }
        mock_client = MagicMock()
        mock_client.complete.return_value = {"content": "cheap"}
        mock_get_client.return_value = mock_client

        result = complete_tier("cheap", "test prompt")

        mock_client.complete.assert_called_once_with(
            prompt="test prompt",
            system=None,
            model="deepseek-v4-flash",
            temperature=0.7,
            max_tokens=4096,
            thinking=False,
        )
        assert result == {"content": "cheap"}

    @patch("src.llm.cascade.get_client")
    @patch("src.llm.cascade.get_model_for_tier")
    def test_routes_to_cross_judge(self, mock_get_tier, mock_get_client):
        mock_get_tier.return_value = {
            "provider": "kimi",
            "model_name": "kimi-k3",
            "thinking": False,
            "temperature": 0.0,
            "max_tokens": 4096,
            "input_cost_per_1m": 3.00,
            "output_cost_per_1m": 15.00,
        }
        mock_client = MagicMock()
        mock_client.complete.return_value = {"content": "judge"}
        mock_get_client.return_value = mock_client

        result = complete_tier("cross_judge", "judge this")

        mock_client.complete.assert_called_once_with(
            prompt="judge this",
            system=None,
            model="kimi-k3",
            temperature=0.0,
            max_tokens=4096,
            thinking=False,
        )
        assert result == {"content": "judge"}

    @patch("src.llm.cascade.get_client")
    @patch("src.llm.cascade.get_model_for_tier")
    def test_falls_back_to_kimi_on_rate_limit(self, mock_get_tier, mock_get_client):
        from openai import RateLimitError

        mock_get_tier.return_value = {
            "provider": "deepseek",
            "model_name": "deepseek-v4-pro",
            "thinking": False,
            "temperature": 0.0,
            "max_tokens": 8192,
            "input_cost_per_1m": 0.44,
            "output_cost_per_1m": 0.87,
        }
        mock_client = MagicMock()
        mock_client.complete.side_effect = [
            RateLimitError("rate limited", response=MagicMock(), body=None),
            {"content": "fallback ok"},
        ]
        mock_get_client.return_value = mock_client

        result = complete_tier("mid", "test prompt")

        assert result == {"content": "fallback ok", "fallback_used": True}
        assert mock_client.complete.call_count == 2
        assert mock_client.complete.call_args_list[1].kwargs["model"] == "kimi-k3"

    @patch("src.llm.cascade.get_client")
    @patch("src.llm.cascade.get_model_for_tier")
    def test_no_fallback_for_cross_judge(self, mock_get_tier, mock_get_client):
        from openai import RateLimitError

        mock_get_tier.return_value = {
            "provider": "kimi",
            "model_name": "kimi-k3",
            "thinking": False,
            "temperature": 0.0,
            "max_tokens": 4096,
            "input_cost_per_1m": 3.00,
            "output_cost_per_1m": 15.00,
        }
        mock_client = MagicMock()
        mock_client.complete.side_effect = RateLimitError(
            "rate limited", response=MagicMock(), body=None
        )
        mock_get_client.return_value = mock_client

        with pytest.raises(RateLimitError):
            complete_tier("cross_judge", "judge this")


class TestEstimateCost:
    def test_frontier_1m_tokens(self):
        cost = estimate_cost("frontier", prompt_tokens=1_000_000, completion_tokens=1_000_000)
        assert cost == pytest.approx(0.44 + 0.87)

    def test_cheap_1m_tokens(self):
        cost = estimate_cost("cheap", prompt_tokens=1_000_000, completion_tokens=1_000_000)
        assert cost == pytest.approx(0.14 + 0.28)

    def test_cross_judge_1m_tokens(self):
        cost = estimate_cost("cross_judge", prompt_tokens=1_000_000, completion_tokens=1_000_000)
        assert cost == pytest.approx(3.00 + 15.00)

    def test_thumbnail_vision_1m_tokens(self):
        cost = estimate_cost("thumbnail_vision", prompt_tokens=1_000_000, completion_tokens=1_000_000)
        assert cost == pytest.approx(0.95 + 4.00)

    def test_zero_tokens(self):
        cost = estimate_cost("frontier", prompt_tokens=0, completion_tokens=0)
        assert cost == 0.0

    def test_small_fractional(self):
        cost = estimate_cost("cheap", prompt_tokens=1000, completion_tokens=500)
        expected = (1000 / 1_000_000 * 0.14) + (500 / 1_000_000 * 0.28)
        assert cost == pytest.approx(expected)

    def test_unknown_tier_raises(self):
        with pytest.raises(ValueError, match="Unknown tier"):
            estimate_cost("bogus", 100, 50)


# ---------------------------------------------------------------------------
# Singleton get_client
# ---------------------------------------------------------------------------

class TestGetClient:
    @patch("src.llm.client.get_config")
    def test_returns_singleton(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        import src.llm.client as client_module

        client_module._client = None
        c1 = get_client()
        c2 = get_client()
        assert c1 is c2


# ---------------------------------------------------------------------------
# Thinking toggle edge cases
# ---------------------------------------------------------------------------

class TestThinkingToggle:
    @patch("src.llm.client.get_config")
    def test_thinking_only_sent_for_deepseek_v4_pro(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()

        models_and_expectation = [
            ("deepseek-v4-pro", True),
            ("deepseek-v4-flash", False),
            ("kimi-k3", False),
            ("kimi-k2.6", False),
        ]

        for model, should_have_thinking in models_and_expectation:
            fake = _fake_completion("ok")
            mock_create = MagicMock(return_value=fake)
            client._get_client(
                "deepseek" if model.startswith("deepseek") else "kimi"
            ).chat.completions.create = mock_create

            client.complete(prompt="test", model=model, thinking=True)

            has_thinking = "extra_body" in mock_create.call_args.kwargs
            assert has_thinking is should_have_thinking, f"Model {model}: expected thinking={should_have_thinking}, got {has_thinking}"

    @patch("src.llm.client.get_config")
    def test_thinking_off_no_extra_body(self, mock_get_config):
        mock_cfg = MagicMock()
        mock_cfg.deepseek.api_key = "sk-test"
        mock_cfg.deepseek.base_url = "https://api.deepseek.com/v1"
        mock_cfg.kimi.api_key = "sk-test"
        mock_cfg.kimi.base_url = "https://api.moonshot.ai/v1"
        mock_get_config.return_value = mock_cfg

        client = LLMClient()
        fake = _fake_completion("ok")
        mock_create = MagicMock(return_value=fake)
        client._get_client("deepseek").chat.completions.create = mock_create

        client.complete(prompt="test", model="deepseek-v4-pro", thinking=False)

        assert "extra_body" not in mock_create.call_args.kwargs