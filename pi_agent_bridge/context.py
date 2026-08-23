"""Task-local context snapshots and Pi prompt composition."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TaskContext:
    """Immutable task context passed from AstrBot into the worker registry."""

    owner_key: str
    session_origin: str
    workspace: str | None = None
    persona: str | None = None
    conversation: dict[str, Any] = field(default_factory=dict)
    media_references: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner_key": self.owner_key,
            "session_origin": self.session_origin,
            "workspace": self.workspace,
            "persona": self.persona,
            "conversation": dict(self.conversation),
            "media_references": list(self.media_references),
            "warnings": list(self.warnings),
        }


def capture_task_context(
    event: Any,
    *,
    workspace: str | None = None,
    persona: str | None = None,
    conversation: Mapping[str, Any] | None = None,
    media_references: list[str] | tuple[str, ...] | None = None,
) -> TaskContext:
    """Capture only public event fields at task creation time."""

    origin = event_owner_key(event)
    return TaskContext(
        owner_key=origin,
        session_origin=origin,
        workspace=workspace,
        persona=persona,
        conversation=dict(conversation or snapshot_event_context(event)),
        media_references=tuple(media_references or ()),
    )


def event_owner_key(event: Any) -> str:
    """Build a stable owner key without relying on AstrBot private state."""

    origin = getattr(event, "unified_msg_origin", None)
    if callable(origin):
        origin = origin()
    if isinstance(origin, str) and origin:
        return origin
    platform = _call(event, "get_platform_name", "unknown")
    group_id = _call(event, "get_group_id", "")
    sender_id = _call(event, "get_sender_id", "")
    return f"{platform}:{group_id}:{sender_id}"


def snapshot_event_context(event: Any) -> dict[str, Any]:
    """Copy public event fields once at task creation time."""

    message = getattr(event, "message_str", "")
    return {
        "unified_msg_origin": event_owner_key(event),
        "platform": _call(event, "get_platform_name", "unknown"),
        "group_id": _call(event, "get_group_id", ""),
        "sender_id": _call(event, "get_sender_id", ""),
        "source_message": message if isinstance(message, str) else "",
    }


def build_pi_prompt(
    task: str,
    *,
    context_snapshot: Mapping[str, Any] | None = None,
    persona: str | None = None,
    media_references: list[str] | None = None,
) -> str:
    """Compose an explicit, immutable initial prompt for an isolated Pi task."""

    sections = [
        "You are a delegated long-running worker. Complete the assigned task in the current workspace.",
        "Do not assume you will receive later chat messages unless they arrive as an explicit follow-up.",
    ]
    if persona:
        sections.append(f"Inherited assistant persona:\n{persona.strip()}")
    if context_snapshot:
        safe_context = {
            key: value
            for key, value in context_snapshot.items()
            if key not in {"credentials", "api_key", "token", "key"}
            and not str(key).startswith("_")
        }
        sections.append(
            "Conversation snapshot at delegation time:\n"
            + json.dumps(safe_context, ensure_ascii=False, sort_keys=True)
        )
    if media_references:
        sections.append(
            "Media references available to this task:\n"
            + "\n".join(f"- {item}" for item in media_references)
        )
    sections.append(f"Assigned task:\n{task.strip()}")
    return "\n\n".join(sections)


def _call(instance: Any, name: str, default: Any) -> Any:
    method = getattr(instance, name, None)
    if not callable(method):
        return default
    try:
        return method()
    except Exception:  # noqa: BLE001
        return default
