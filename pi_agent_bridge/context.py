"""Minimal task identity and Pi request prompt construction."""

from __future__ import annotations

from typing import Any


def event_owner_key(event: Any) -> str:
    """Build the stable AstrBot session owner key for task permissions."""

    origin = getattr(event, "unified_msg_origin", None)
    if callable(origin):
        origin = origin()
    if isinstance(origin, str) and origin:
        return origin
    platform = _call(event, "get_platform_name", "unknown")
    group_id = _call(event, "get_group_id", "")
    sender_id = _call(event, "get_sender_id", "")
    return f"{platform}:{group_id}:{sender_id}"


def build_pi_prompt(
    task: str,
    *,
    context_snapshot: Any | None = None,
    persona: Any | None = None,
    media_references: Any | None = None,
) -> str:
    """Build a Pi prompt from only the main model's refined task request."""

    del context_snapshot, persona, media_references
    return (
        "You are a delegated Agent worker. Execute the assigned request in the "
        "current workspace. Do not assume later chat messages unless AstrBot "
        "sends an explicit follow-up.\n\n"
        f"Assigned request:\n{task.strip()}"
    )


def _call(instance: Any, name: str, default: Any) -> Any:
    method = getattr(instance, name, None)
    if not callable(method):
        return default
    try:
        return method()
    except Exception:  # noqa: BLE001
        return default


__all__ = ["build_pi_prompt", "event_owner_key"]
