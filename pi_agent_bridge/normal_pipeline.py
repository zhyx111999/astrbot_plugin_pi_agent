"""Public AstrBot normal-pipeline relay for terminal Pi wakeups."""

from __future__ import annotations

from typing import Any


class NormalPipelineRelayError(RuntimeError):
    """The host cannot enqueue a synthetic normal-pipeline event."""


async def enqueue_terminal_wakeup(
    *,
    context: Any,
    session_origin: str,
    message: str,
) -> None:
    """Submit a terminal wake message to AstrBot's normal event queue.

    This uses AstrBot's public StarTools event factory instead of sending a
    message directly. The resulting wake event is processed by the regular
    preprocess/agent/respond pipeline, while the Pi session itself remains
    available only through the plugin's LLM tools.
    """

    del context
    try:
        from astrbot.api.message_components import Plain
        from astrbot.api.platform import MessageMember
        from astrbot.api.star import StarTools
        from astrbot.core.platform.message_session import MessageSession
    except (ImportError, ModuleNotFoundError) as exc:
        raise NormalPipelineRelayError(
            "AstrBot public normal-pipeline event APIs are unavailable"
        ) from exc

    try:
        session = MessageSession.from_str(session_origin)
    except Exception as exc:  # noqa: BLE001
        raise NormalPipelineRelayError(
            f"invalid task session origin: {session_origin}"
        ) from exc

    if not callable(getattr(StarTools, "create_message", None)) or not callable(
        getattr(StarTools, "create_event", None)
    ):
        raise NormalPipelineRelayError(
            "AstrBot StarTools.create_message/create_event is unavailable"
        )

    message_obj = await StarTools.create_message(
        type=session.message_type.value,
        self_id="astrbot",
        session_id=session.session_id,
        sender=MessageMember(user_id="astrbot", nickname="AstrBot Pi Agent"),
        message=[Plain(message)],
        message_str=message,
        raw_message={
            "origin": "astrbot_plugin_pi_agent",
            "kind": "terminal_wakeup",
        },
        group_id=(
            session.session_id
            if session.message_type.value == "GroupMessage"
            else ""
        ),
    )
    await StarTools.create_event(
        message_obj,
        platform=session.platform_id,
        is_wake=True,
    )


__all__ = ["NormalPipelineRelayError", "enqueue_terminal_wakeup"]
