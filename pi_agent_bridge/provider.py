"""Secret-free mapping from AstrBot providers to Pi model configuration."""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class PiModelSettings:
    """Explicit Pi runtime controls owned by the plugin configuration."""

    thinking_level: str = "medium"
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_modalities: tuple[str, ...] = ("text", "image")
    temperature: float | None = 0.5
    top_p: float | None = 1.0
    top_k: int | None = None
    min_p: float | None = None
    sampling_params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        level = str(self.thinking_level or "medium").strip().lower()
        if level not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"unsupported pi_thinking_level: {level}")
        modalities = tuple(
            item for item in (str(value).strip().lower() for value in self.input_modalities)
            if item in {"text", "image"}
        )
        if "text" not in modalities:
            modalities = ("text", *modalities)
        object.__setattr__(self, "thinking_level", level)
        object.__setattr__(self, "input_modalities", tuple(dict.fromkeys(modalities)))
        for name in ("context_window", "max_output_tokens", "top_k"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or int(value) <= 0):
                object.__setattr__(self, name, None)
        for name in ("temperature", "top_p", "min_p"):
            value = getattr(self, name)
            if value is not None:
                numeric = float(value)
                if name == "min_p" and numeric <= 0:
                    numeric = None
                object.__setattr__(self, name, numeric)
        object.__setattr__(self, "sampling_params", _json_mapping(self.sampling_params))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PiModelSettings":
        return cls(
            thinking_level=value.get("thinking_level", "medium"),
            context_window=_positive_int(value.get("context_window")),
            max_output_tokens=_positive_int(value.get("max_output_tokens")),
            input_modalities=tuple(value.get("input_modalities") or ("text", "image")),
            temperature=_number_or_none(value.get("temperature")),
            top_p=_number_or_none(value.get("top_p")),
            top_k=_positive_int(value.get("top_k")),
            min_p=_number_or_none(value.get("min_p")),
            sampling_params=value.get("sampling_params") or {},
        )

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "PiModelSettings":
        return cls(
            thinking_level=config.get("pi_thinking_level", "medium"),
            context_window=_positive_int(config.get("pi_context_window")),
            max_output_tokens=_positive_int(config.get("pi_max_output_tokens")),
            input_modalities=tuple(config.get("pi_input_modalities") or ("text", "image")),
            temperature=_number_or_none(config.get("pi_temperature", 0.5)),
            top_p=_number_or_none(config.get("pi_top_p", 1.0)),
            top_k=_positive_int(config.get("pi_top_k")),
            min_p=_number_or_none(config.get("pi_min_p")),
            sampling_params=_sampling_config(config.get("pi_sampling_params")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "thinking_level": self.thinking_level,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "input_modalities": list(self.input_modalities),
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "min_p": self.min_p,
            "sampling_params": dict(self.sampling_params),
        }


def _sampling_config(value: Any) -> Mapping[str, Any]:
    if not value:
        return {}
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("pi_sampling_params must be valid JSON") from exc
        if isinstance(parsed, Mapping):
            return parsed
    raise ValueError("pi_sampling_params must be a JSON object")


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
def build_provider_binding(
    *,
    provider_id: str,
    provider: Any,
    agent_dir: str | os.PathLike[str],
    model_settings: PiModelSettings | None = None,
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
    model = _model(provider, meta, config)
    settings = model_settings or PiModelSettings()
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
            settings=settings,
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
    settings: PiModelSettings,
) -> dict[str, Any]:
    """Build Pi model metadata exclusively from explicit plugin settings."""

    entry: dict[str, Any] = {
        "id": model,
        "input": list(settings.input_modalities),
        "reasoning": settings.thinking_level != "off",
    }
    if settings.context_window is not None:
        entry["contextWindow"] = settings.context_window
    if settings.max_output_tokens is not None:
        entry["maxTokens"] = settings.max_output_tokens
    sampling = dict(settings.sampling_params)
    if settings.temperature is not None:
        sampling["temperature"] = settings.temperature
    if settings.top_p is not None:
        sampling["topP"] = settings.top_p
    if settings.top_k is not None:
        sampling["topK"] = settings.top_k
    if settings.min_p is not None:
        sampling["minP"] = settings.min_p
    if sampling:
        entry["samplingParams"] = sampling
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


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _model(provider: Any, meta: Any, config: Mapping[str, Any]) -> str:
    model = str(getattr(meta, "model", "") or "").strip()
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
    "safe_error_summary",
    "write_models_json",
]
