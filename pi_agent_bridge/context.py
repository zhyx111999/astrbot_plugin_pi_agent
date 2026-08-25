"""Minimal task identity and Pi request prompt construction."""

from __future__ import annotations

from typing import Any


def event_session_origin(event: Any) -> str:
    """Return the conversation target used for task progress and terminal wakeups."""

    origin = getattr(event, "unified_msg_origin", None)
    if callable(origin):
        origin = origin()
    if isinstance(origin, str) and origin:
        return origin
    platform = _call(event, "get_platform_id", None) or _call(
        event, "get_platform_name", "unknown"
    )
    group_id = _call(event, "get_group_id", "")
    sender_id = _call(event, "get_sender_id", "")
    message_type = "GroupMessage" if group_id else "FriendMessage"
    session_id = group_id or sender_id
    return f"{platform}:{message_type}:{session_id}"


def event_owner_key(event: Any) -> str:
    """Build a stable platform-user identity for task permissions."""

    platform = _call(event, "get_platform_id", None) or _call(
        event, "get_platform_name", None
    )
    sender_id = _call(event, "get_sender_id", None)
    if platform and sender_id:
        return f"{platform}:{sender_id}"

    # Minimal test doubles and old host adapters may expose only the origin.
    # Real AstrBot events provide both platform and sender identifiers.
    origin = event_session_origin(event)
    return origin


def build_pi_prompt(task: str) -> str:
    """Build a Pi prompt from only the main model's refined task request."""

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


__all__ = ["build_pi_prompt", "event_owner_key", "event_session_origin"]
