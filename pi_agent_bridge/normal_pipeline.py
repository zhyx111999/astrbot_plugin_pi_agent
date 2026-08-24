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


async def enqueue_terminal_wakeup(
    *,
    context: Any,
    session_origin: str,
    message: str,
) -> None:
    """Submit a terminal wake message to AstrBot's normal event queue.

    The relay uses only AstrBot's public StarTools and platform APIs. The
    resulting wake event is processed by the regular preprocess/agent/respond
    pipeline, while the Pi session remains available only through the plugin's
    LLM tools.
    """

    del context
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
        sender=MessageMember(
            user_id="astrbot_pi_agent",
            nickname="AstrBot Pi Agent",
        ),
        message=[Plain(message)],
        message_str=message,
        raw_message={
            "origin": "astrbot_plugin_pi_agent",
            "kind": "terminal_wakeup",
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


__all__ = ["NormalPipelineRelayError", "enqueue_terminal_wakeup"]
