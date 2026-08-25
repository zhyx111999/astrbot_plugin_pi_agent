"""Public AstrBot normal-pipeline relay for terminal Pi wakeups."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class NormalPipelineRelayError(RuntimeError):
    """The host cannot enqueue a synthetic normal-pipeline event."""


@dataclass(frozen=True, slots=True)
class _SessionOrigin:
    platform_id: str
    message_type: str
    session_id: str


def _parse_session_origin(value: str) -> _SessionOrigin:
    parts = value.split(":", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise NormalPipelineRelayError(f"invalid task session origin: {value}")
    return _SessionOrigin(*(part.strip() for part in parts))


async def enqueue_task_wakeup(
    *,
    context: Any,
    session_origin: str,
    message: str,
    kind: str,
) -> None:
    """Submit a task update to AstrBot's normal event queue.

    The relay uses only AstrBot's public StarTools and platform APIs. The
    resulting wake event is processed by the regular preprocess/agent/respond
    pipeline, while the Pi session remains available only through the plugin's
    LLM tools.
    """

    try:
        from astrbot.api.message_components import Plain
        from astrbot.api.platform import MessageMember
        from astrbot.api.star import StarTools
    except (ImportError, ModuleNotFoundError) as exc:
        raise NormalPipelineRelayError(
            "AstrBot public normal-pipeline event APIs are unavailable"
        ) from exc

    session = _parse_session_origin(session_origin)
    if not callable(getattr(StarTools, "create_message", None)) or not callable(
        getattr(StarTools, "create_event", None)
    ):
        raise NormalPipelineRelayError(
            "AstrBot StarTools.create_message/create_event is unavailable"
        )

    message_obj = await StarTools.create_message(
        type=session.message_type,
        self_id="astrbot",
        session_id=session.session_id,
        # AstrBot's aiocqhttp event sender is also used by the normal
        # response adapter as the private-chat destination. Keep the original
        # session user here; a synthetic sender ID would make event.send()
        # target the non-numeric ``astrbot_pi_agent`` marker instead of the
        # actual user. The callback marker belongs in raw_message, not in the
        # visible sender identity; otherwise the model receives an internal
        # looking user ID and may treat the wakeup as non-user-facing.
        sender=MessageMember(
            user_id=session.session_id,
            nickname="用户",
        ),
        message=[Plain(message)],
        message_str=message,
        raw_message={
            "origin": "astrbot_plugin_pi_agent",
            "kind": kind,
        },
        group_id=(
            session.session_id
            if session.message_type == "GroupMessage"
            else ""
        ),
    )
    await StarTools.create_event(
        message_obj,
        platform=session.platform_id,
        is_wake=True,
    )


async def enqueue_terminal_wakeup(
    *,
    context: Any,
    session_origin: str,
    message: str,
) -> None:
    """Submit a terminal task wakeup to AstrBot's normal event queue."""

    await enqueue_task_wakeup(
        context=context,
        session_origin=session_origin,
        message=message,
        kind="terminal_wakeup",
    )


async def enqueue_progress_wakeup(
    *,
    context: Any,
    session_origin: str,
    message: str,
) -> None:
    """Submit a bounded intermediate task update to AstrBot's normal queue."""

    await enqueue_task_wakeup(
        context=context,
        session_origin=session_origin,
        message=message,
        kind="progress_wakeup",
    )


__all__ = [
    "NormalPipelineRelayError",
    "enqueue_progress_wakeup",
    "enqueue_task_wakeup",
    "enqueue_terminal_wakeup",
]
