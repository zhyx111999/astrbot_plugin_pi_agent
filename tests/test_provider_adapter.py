"""Tests for the secret-free AstrBot provider adapter."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pi_agent_bridge.provider import (
    PiProviderError,
    build_provider_binding,
    resolve_provider_id,
    safe_error_summary,
)


class OpenAICompatibleProvider:
    provider_config = {
        "type": "openai_chat_completion",
        "api_base": "https://gateway.example/v1",
        "custom_headers": {"X-Workspace": "tenant-secret"},
        "modalities": ["text", "image", "tool_use"],
        "max_context_tokens": 64000,
        "custom_extra_body": {
            "temperature": 0.2,
            "reasoning_effort": "high",
            "max_tokens": 4096,
        },
        "cost": {"input": 1, "output": 2},
        "compat": {"supportsDeveloperRole": False},
    }

    def meta(self):
        return SimpleNamespace(model="gpt-test", type="openai_chat_completion")

    def get_current_key(self):
        return "api-secret"


def test_binding_writes_only_variable_references(tmp_path):
    binding = build_provider_binding(
        provider_id="gateway/main",
        provider=OpenAICompatibleProvider(),
        agent_dir=tmp_path / "agent",
    )

    models = (tmp_path / "agent" / "models.json").read_text(encoding="utf-8")
    payload = json.loads(models)
    entry = payload["providers"][binding.pi_provider_id]
    model_entry = entry["models"][0]
    assert binding.model == "gpt-test"
    assert binding.environment["PI_ASTRBOT_API_KEY"] == "api-secret"
    assert binding.environment["PI_CODING_AGENT_DIR"] == str(
        (tmp_path / "agent").resolve()
    )
    assert entry["apiKey"] == "$PI_ASTRBOT_API_KEY"
    assert model_entry["input"] == ["text", "image"]
    assert model_entry["contextWindow"] == 64000
    assert model_entry["maxTokens"] == 4096
    assert model_entry["reasoning"] is True
    assert model_entry["samplingParams"] == {
        "temperature": 0.2,
        "reasoning_effort": "high",
        "max_tokens": 4096,
    }
    assert model_entry["cost"] == {"input": 1, "output": 2}
    assert model_entry["compat"] == {"supportsDeveloperRole": False}
    assert "api-secret" not in models
    assert "tenant-secret" not in models


@pytest.mark.asyncio
async def test_empty_config_uses_current_chat_provider_id():
    class Context:
        async def get_current_chat_provider_id(self, umo):
            assert umo == "qq:1"
            return "chat-provider"

    assert await resolve_provider_id(Context(), "", "qq:1") == "chat-provider"


def test_non_openai_provider_is_rejected(tmp_path):
    class Provider:
        provider_config = {
            "type": "anthropic",
            # A base URL alone must not make a non-OpenAI wire protocol
            # eligible for the OpenAI adapter.
            "api_base": "https://anthropic.example/v1",
        }

        def meta(self):
            return SimpleNamespace(model="claude", type="anthropic")

        def get_current_key(self):
            return "secret"

    with pytest.raises(PiProviderError, match="OpenAI-compatible"):
        build_provider_binding(
            provider_id="anthropic", provider=Provider(), agent_dir=tmp_path / "agent"
        )


def test_header_environment_name_collision_is_rejected(tmp_path):
    class Provider:
        provider_config = {
            "type": "openai_chat_completion",
            "custom_headers": {"X-Foo": "one", "X_Foo": "two"},
        }

        def meta(self):
            return SimpleNamespace(model="gpt-test", type="openai_chat_completion")

        def get_current_key(self):
            return "api-secret"

    with pytest.raises(PiProviderError, match="collide"):
        build_provider_binding(
            provider_id="gateway", provider=Provider(), agent_dir=tmp_path / "agent"
        )


def test_inline_credentials_in_base_url_are_rejected(tmp_path):
    class Provider:
        provider_config = {
            "type": "openai_chat_completion",
            "api_base": "https://gateway.example/v1?api_key=inline-secret",
        }

        def meta(self):
            return SimpleNamespace(model="gpt-test", type="openai_chat_completion")

        def get_current_key(self):
            return "api-secret"

    with pytest.raises(PiProviderError, match="inline credentials"):
        build_provider_binding(
            provider_id="gateway", provider=Provider(), agent_dir=tmp_path / "agent"
        )


def test_provider_exception_summary_does_not_echo_secret(tmp_path):
    secret = "sk-live-provider-secret"

    class Provider:
        provider_config = {"type": "openai_chat_completion"}

        def meta(self):
            raise RuntimeError(f"request failed with {secret}")

    with pytest.raises(PiProviderError) as caught:
        build_provider_binding(
            provider_id="gateway", provider=Provider(), agent_dir=tmp_path / "agent"
        )
    assert secret not in str(caught.value)


def test_credential_exception_summary_does_not_echo_secret(tmp_path):
    secret = "sk-live-provider-secret"

    class Provider:
        provider_config = {"type": "openai_chat_completion"}

        def meta(self):
            return SimpleNamespace(model="gpt-test", type="openai_chat_completion")

        def get_current_key(self):
            raise RuntimeError(f"key backend failed: {secret}")

    with pytest.raises(PiProviderError) as caught:
        build_provider_binding(
            provider_id="gateway", provider=Provider(), agent_dir=tmp_path / "agent"
        )
    assert secret not in str(caught.value)


def test_safe_error_summary_masks_credentials_and_bounds_output():
    secret = "sk-live-provider-secret"
    summary = safe_error_summary(
        f"POST https://gateway.example/v1?api_key={secret} authorization=Bearer {secret}",
        secrets=[secret],
        max_length=80,
    )
    assert secret not in summary
    assert "[REDACTED]" in summary
    assert len(summary) <= 80


def test_binding_repr_does_not_include_child_environment_secret(tmp_path):
    binding = build_provider_binding(
        provider_id="gateway/main",
        provider=OpenAICompatibleProvider(),
        agent_dir=tmp_path / "agent",
    )
    assert "api-secret" not in repr(binding)


def test_responses_provider_writes_the_responses_api_name(tmp_path):
    class Provider:
        provider_config = {
            "type": "openai_responses",
            "api_base": "https://gateway.example/v1",
        }

        def meta(self):
            return SimpleNamespace(model="gpt-responses", type="openai_responses")

        def get_current_key(self):
            return "responses-secret"

    binding = build_provider_binding(
        provider_id="responses", provider=Provider(), agent_dir=tmp_path / "agent"
    )
    payload = json.loads(
        (tmp_path / "agent" / "models.json").read_text(encoding="utf-8")
    )

    assert binding.model == "gpt-responses"
    assert payload["providers"][binding.pi_provider_id]["api"] == "openai-responses"
    assert "responses-secret" not in json.dumps(payload)


def test_provider_config_failure_is_summarized_without_raw_exception(tmp_path):
    secret = "sk-provider-config-secret"

    class Provider:
        @property
        def provider_config(self):
            raise RuntimeError(f"config backend failed with {secret}")

        def meta(self):
            return SimpleNamespace(model="gpt-test", type="openai_chat_completion")

        def get_current_key(self):
            return secret

    with pytest.raises(PiProviderError) as caught:
        build_provider_binding(
            provider_id="gateway", provider=Provider(), agent_dir=tmp_path / "agent"
        )
    assert secret not in str(caught.value)
