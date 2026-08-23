"""Secret-free mapping from AstrBot providers to Pi model configuration."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from urllib.parse import parse_qsl, urlsplit
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .security import safe_error_summary


class PiProviderError(RuntimeError):
    """The selected AstrBot provider cannot be represented by Pi yet."""


@dataclass(frozen=True, slots=True)
class PiProviderBinding:
    """Per-worker values; the secret exists only in the child environment."""

    source_provider_id: str
    pi_provider_id: str
    model: str
    agent_dir: str
    # Never include child credentials in an accidental dataclass repr/log.
    environment: dict[str, str] = field(repr=False, compare=False)


# AstrBot adapter ids are intentionally allow-listed.  Merely having an
# ``api_base`` field is not sufficient: Anthropic and several other adapters
# also expose that field but use a different wire protocol.
_OPENAI_COMPATIBLE_TYPES = frozenset(
    {
        "openai_chat_completion",
        "openai_responses",
        "openrouter_chat_completion",
        "groq_chat_completion",
        "xai_chat_completion",
        "aihubmix_chat_completion",
        "ssycloud_chat_completion",
        "zhipu_chat_completion",
        "longcat_chat_completion",
        "xiaomi_chat_completion",
    }
)
_RESPONSES_TYPES = frozenset({"openai_responses"})
async def resolve_provider_id(context: Any, configured_id: str | None, umo: str) -> str:
    """Resolve the configured provider or the model selected for this chat."""

    provider_id = str(configured_id or "").strip()
    if provider_id:
        return provider_id
    getter = getattr(context, "get_current_chat_provider_id", None)
    if not callable(getter):
        raise PiProviderError("AstrBot provider selection API is unavailable")
    result = getter(umo)
    if asyncio.iscoroutine(result):
        result = await result
    if not isinstance(result, str) or not result.strip():
        raise PiProviderError("AstrBot did not return a chat provider id")
    return result.strip()


def build_provider_binding(
    *,
    provider_id: str,
    provider: Any,
    agent_dir: str | os.PathLike[str],
    model_override: str | None = None,
) -> PiProviderBinding:
    """Create a Pi OpenAI-compatible provider entry without writing a key."""

    config = _provider_config(provider)
    meta = _provider_meta(provider)
    provider_type = str(config.get("type") or getattr(meta, "type", "") or "")
    if not _is_openai_compatible(provider_type, type(provider).__name__):
        raise PiProviderError(
            "The selected AstrBot provider is not supported by the initial Pi "
            "bridge; OpenAI-compatible providers only"
        )
    model = _model(provider, meta, config, model_override)
    pi_provider_id = f"astrbot-{_safe_id(provider_id)}"
    path = Path(agent_dir).expanduser().resolve(strict=False)
    # Pi 0.84.x reads models.json from this exact environment-controlled agent
    # directory.  Keep the directory worker-local so concurrent tasks never
    # race on a shared global models.json.
    environment = {
        "PI_ASTRBOT_API_KEY": _provider_key(provider),
        "PI_CODING_AGENT_DIR": str(path),
    }
    entry: dict[str, Any] = {
        "baseUrl": _safe_base_url(
            _first_string(config, "api_base", "base_url", "baseUrl")
            or "https://api.openai.com/v1"
        ),
        "api": "openai-responses"
        if provider_type.strip().lower() in _RESPONSES_TYPES
        else "openai-completions",
        "apiKey": "$PI_ASTRBOT_API_KEY",
        "models": [_build_model_entry(
            model=model,
            provider=provider,
            meta=meta,
            config=config,
        )],
    }
    headers = _header_refs(config, environment)
    if headers:
        entry["headers"] = headers
    write_models_json(path, pi_provider_id, entry)
    return PiProviderBinding(
        source_provider_id=provider_id,
        pi_provider_id=pi_provider_id,
        model=model,
        agent_dir=str(path),
        environment=environment,
    )


def write_models_json(agent_dir: Path, provider_id: str, entry: Mapping[str, Any]) -> Path:
    """Atomically merge a variable-referenced provider entry into models.json."""

    agent_dir.mkdir(parents=True, exist_ok=True)
    target = agent_dir / "models.json"
    payload: dict[str, Any] = {"providers": {}}
    if target.is_file():
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PiProviderError(f"Invalid Pi models.json: {target}") from exc
        if isinstance(current, dict):
            payload = current
    providers = payload.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise PiProviderError("Pi models.json providers must be an object")
    providers[provider_id] = dict(entry)
    descriptor, temporary = tempfile.mkstemp(prefix="models-", suffix=".json", dir=agent_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def _build_model_entry(
    *,
    model: str,
    provider: Any,
    meta: Any,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Map AstrBot's public provider/model values to Pi model metadata.

    Missing values are intentionally omitted so Pi applies its own defaults;
    AstrBot's unset modalities retain its backward-compatible text/image input
    behavior because Pi does not support audio or video model inputs.
    """

    entry: dict[str, Any] = {"id": model}
    modalities = config.get("modalities")
    if modalities is None or modalities == []:
        supported_modalities = {"text", "image"}
    elif isinstance(modalities, list):
        supported_modalities = {str(item).strip().lower() for item in modalities}
    else:
        supported_modalities = {"text"}
    input_types = ["text"]
    if "image" in supported_modalities:
        input_types.append("image")
    entry["input"] = input_types

    extra_body = config.get("custom_extra_body")
    if isinstance(extra_body, Mapping) and extra_body:
        sampling_params = _json_mapping(extra_body)
        if sampling_params:
            entry["samplingParams"] = sampling_params
        max_tokens = _positive_int(
            extra_body.get("max_tokens") or extra_body.get("max_output_tokens")
        )
        if max_tokens is not None:
            entry["maxTokens"] = max_tokens
        if extra_body.get("reasoning_effort"):
            entry["reasoning"] = True

    reasoning = config.get("reasoning", getattr(meta, "reasoning", None))
    if isinstance(reasoning, bool):
        entry["reasoning"] = reasoning

    metadata = getattr(meta, "model_metadata", None) or getattr(meta, "metadata", None)
    if isinstance(metadata, Mapping):
        metadata = metadata.get(model, metadata)
    if isinstance(metadata, Mapping):
        if isinstance(metadata.get("modalities"), Mapping):
            metadata_input = metadata["modalities"].get("input")
            if isinstance(metadata_input, list):
                entry["input"] = [
                    item for item in ("text", "image") if item in metadata_input
                ] or ["text"]
        if isinstance(metadata.get("reasoning"), bool):
            entry["reasoning"] = metadata["reasoning"]
        limit = metadata.get("limit")
        if isinstance(limit, Mapping):
            context = _positive_int(limit.get("context"))
            output = _positive_int(limit.get("output"))
            if context is not None:
                entry["contextWindow"] = context
            if output is not None:
                entry["maxTokens"] = output
        for source, target in (
            ("contextWindow", "contextWindow"),
            ("maxTokens", "maxTokens"),
            ("max_tokens", "maxTokens"),
        ):
            value = _positive_int(metadata.get(source))
            if value is not None:
                entry[target] = value

    context_window = _positive_int(
        config.get("max_context_tokens")
        or config.get("context_window")
        or config.get("contextWindow")
    )
    if context_window is not None:
        entry["contextWindow"] = context_window

    for key in ("cost", "compat"):
        value = config.get(key)
        if isinstance(value, Mapping) and value:
            entry[key] = _json_mapping(value)
    return entry


def _json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        try:
            json.dumps(item)
        except (TypeError, ValueError):
            continue
        result[str(key)] = item
    return result


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
def _provider_config(provider: Any) -> Mapping[str, Any]:
    try:
        value = getattr(provider, "provider_config", None)
    except Exception as exc:  # noqa: BLE001
        # A provider property may make a backend request and include headers
        # or credentials in its exception text. Keep that detail out of the
        # structured result returned to the host model.
        raise PiProviderError("Unable to read AstrBot provider configuration") from exc
    return value if isinstance(value, Mapping) else {}


def _provider_meta(provider: Any) -> Any:
    getter = getattr(provider, "meta", None)
    if not callable(getter):
        raise PiProviderError("AstrBot provider metadata API is unavailable")
    try:
        return getter()
    except Exception as exc:  # noqa: BLE001
        # Provider exceptions can contain request URLs, authorization headers,
        # or raw credentials.  Keep the public error deliberately generic.
        raise PiProviderError("Unable to read AstrBot provider metadata") from exc


def _model(provider: Any, meta: Any, config: Mapping[str, Any], override: str | None) -> str:
    model = str(override or getattr(meta, "model", "") or "").strip()
    if not model:
        getter = getattr(provider, "get_model", None)
        if callable(getter):
            try:
                model = str(getter() or "").strip()
            except Exception as exc:  # noqa: BLE001
                raise PiProviderError("Unable to read AstrBot provider model") from exc
    if not model:
        model = str(config.get("model") or "").strip()
    if not model:
        raise PiProviderError("AstrBot provider has no selected model")
    return model


def _provider_key(provider: Any) -> str:
    getter = getattr(provider, "get_current_key", None)
    if callable(getter):
        try:
            value = getter()
        except Exception as exc:  # noqa: BLE001
            raise PiProviderError("Unable to read AstrBot provider credentials") from exc
        if isinstance(value, str) and value:
            return value
    getter = getattr(provider, "get_keys", None)
    if callable(getter):
        try:
            values = getter()
        except Exception as exc:  # noqa: BLE001
            raise PiProviderError("Unable to read AstrBot provider credentials") from exc
        if isinstance(values, list) and values and isinstance(values[0], str):
            return values[0]
    raise PiProviderError("AstrBot provider has no usable API key")


def _header_refs(config: Mapping[str, Any], environment: dict[str, str]) -> dict[str, str]:
    headers = config.get("custom_headers") or config.get("headers") or {}
    if not isinstance(headers, Mapping):
        return {}
    result: dict[str, str] = {}
    assigned: set[str] = set(environment)
    for name, value in headers.items():
        if isinstance(value, str) and value:
            variable = _safe_env_name(f"PI_ASTRBOT_HEADER_{name}")
            if variable in assigned:
                # Header names are user-controlled and can normalize to the
                # same environment variable (e.g. ``X-Foo``/``X_Foo``).
                raise PiProviderError(
                    "AstrBot custom header names collide after environment normalization"
                )
            assigned.add(variable)
            environment[variable] = value
            result[str(name)] = f"${variable}"
    return result


def _is_openai_compatible(provider_type: str, class_name: str) -> bool:
    """Return true only for known AstrBot OpenAI-wire-compatible adapters."""

    normalized = provider_type.strip().lower()
    if normalized in _OPENAI_COMPATIBLE_TYPES:
        return True
    # Keep lightweight third-party/test adapters usable when they explicitly
    # identify themselves as OpenAI-compatible.  A base URL alone never opts
    # an adapter in.
    return "openai" in class_name.lower() and normalized in {
        "openai",
        "openai_compatible",
        "openai-compatible",
    }


def _first_string(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _safe_base_url(value: str) -> str:
    """Reject inline query credentials before writing the durable models file."""

    try:
        parts = urlsplit(value)
        query = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError as exc:
        raise PiProviderError("AstrBot provider base URL is invalid") from exc
    if parts.username or parts.password:
        raise PiProviderError("AstrBot provider base URL must not contain inline credentials")
    secret_tokens = ("apikey", "accesstoken", "authorization", "password", "secret", "token")
    if any(
        any(token in re.sub(r"[^a-z0-9]", "", name.lower()) for token in secret_tokens)
        for name, _ in query
    ):
        raise PiProviderError("AstrBot provider base URL must not contain inline credentials")
    return value


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return normalized[:80] or "provider"


def _safe_env_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value).upper()


__all__ = [
    "PiProviderBinding",
    "PiProviderError",
    "build_provider_binding",
    "resolve_provider_id",
    "safe_error_summary",
    "write_models_json",
]
