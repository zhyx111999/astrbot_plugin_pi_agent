"""Bounded, credential-aware diagnostics for public bridge envelopes."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|authorization|bearer|token|secret|password)"
    r"(\s*[:=]\s*)(?:bearer\s+)?[^\s,;}'\"]+"
)
_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|authorization|token|secret|password)=)"
    r"[^&\s]+"
)
_BEARER = re.compile(r"(?i)(bearer\s+)[^\s,;}'\"]+")


def safe_error_summary(
    error: BaseException | str,
    *,
    secrets: Iterable[str] = (),
    max_length: int = 512,
) -> str:
    """Return a short diagnostic with credential-shaped values redacted."""

    if max_length < 1:
        raise ValueError("max_length must be positive")
    text = redact_text(str(error).strip() or type(error).__name__, secrets=secrets)
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "..."
    return text


def redact_text(text: str, *, secrets: Iterable[str] = ()) -> str:
    """Mask credentials without shortening regular assistant/tool content."""

    for secret in sorted(
        {value for value in secrets if isinstance(value, str) and value},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "[REDACTED]")
    text = _SECRET_QUERY.sub(r"\1[REDACTED]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    text = _BEARER.sub(r"\1[REDACTED]", text)
    return text


def sanitize_value(value: Any, *, secrets: Iterable[str] = ()) -> Any:
    """Recursively sanitize event values before registry persistence."""

    if isinstance(value, str):
        return redact_text(value, secrets=secrets)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _looks_secret(name):
                result[name] = "[REDACTED]"
            else:
                result[name] = sanitize_value(item, secrets=secrets)
        return result
    if isinstance(value, list):
        return [sanitize_value(item, secrets=secrets) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item, secrets=secrets) for item in value]
    return value


def _looks_secret(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return any(
        token in normalized
        for token in (
            "apikey",
            "accesstoken",
            "authorization",
            "credential",
            "cookie",
            "password",
            "secret",
            "token",
        )
    )


def is_secret_environment_key(name: str) -> bool:
    """Return whether an environment variable likely carries credentials.

    Environment variables use names such as ``PI_API_KEY`` and
    ``PI_SELECTED_KEY``.  The structured-payload classifier intentionally
    does not treat a bare ``key`` field as secret because ordinary JSON often
    contains harmless keys; process environments need the stricter rule.
    """

    normalized = re.sub(r"[^a-z0-9]", "", str(name).lower())
    if _looks_secret(name):
        return True
    return normalized.endswith("key") or normalized.endswith("auth")


__all__ = [
    "is_secret_environment_key",
    "redact_text",
    "safe_error_summary",
    "sanitize_value",
]
