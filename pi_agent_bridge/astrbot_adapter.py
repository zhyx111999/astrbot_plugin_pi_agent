"""Small compatibility facade over AstrBot's documented plugin APIs.

The bridge intentionally receives an AstrBot ``Context`` by duck typing. It
does not import AstrBot internals, construct ``CronMessageEvent`` instances, or
reach into the main-agent implementation. This keeps the Pi worker usable when
AstrBot changes private implementation details between releases.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any


class AstrBotAdapterError(RuntimeError):
    """Base error raised by the public API adapter."""


class UnsupportedAstrBotCapability(AstrBotAdapterError):
    """Raised when a requested capability is not part of the public surface."""


WakeMainAgent = Callable[
    [str, str, Mapping[str, Any] | None],
    Awaitable[Any] | Any,
]


async def _resolve(value: Awaitable[Any] | Any) -> Any:
    """Resolve an async public API result while tolerating sync test doubles."""
    if inspect.isawaitable(value):
        return await value
    return value


class AstrBotAdapter:
    """Expose only documented Context/Star APIs needed by the Pi bridge.

    ``plugin_config`` is the object AstrBot injects into a plugin constructor
    when ``_conf_schema.json`` exists. It is kept intact so callers can still
    use AstrBotConfig methods such as ``save_config()`` when available.
    """

    def __init__(
        self,
        context: Any,
        plugin_config: Mapping[str, Any] | None = None,
        *,
        wake_main_agent: WakeMainAgent | None = None,
    ) -> None:
        self.context = context
        self.plugin_config = plugin_config if plugin_config is not None else {}
        self._wake_main_agent = wake_main_agent

    def get_plugin_config(self, key: str | None = None, default: Any = None) -> Any:
        """Read the plugin-local config injected by AstrBot.

        This is deliberately separate from :meth:`get_core_config`: the latter
        reads AstrBot's global/session config and must not be confused with the
        plugin's ``_conf_schema.json`` data.
        """
        if key is None:
            return self.plugin_config
        getter = getattr(self.plugin_config, "get", None)
        if not callable(getter):
            raise AstrBotAdapterError("plugin config does not provide get()")
        return getter(key, default)

    def get_core_config(self, umo: str | None = None) -> Any:
        """Read AstrBot's public Context configuration getter."""
        getter = getattr(self.context, "get_config", None)
        if not callable(getter):
            raise UnsupportedAstrBotCapability("Context.get_config is unavailable")
        return getter(umo) if umo is not None else getter()

    async def get_provider_id(self, umo: str) -> str:
        """Return the provider selected for a session."""
        method = self._require_context_method("get_current_chat_provider_id")
        return str(await _resolve(method(umo=umo)))

    async def llm_generate(self, **kwargs: Any) -> Any:
        """Call the documented ``Context.llm_generate`` method."""
        method = self._require_context_method("llm_generate")
        return await _resolve(method(**kwargs))

    async def tool_loop_agent(self, **kwargs: Any) -> Any:
        """Call the documented ``Context.tool_loop_agent`` method."""
        method = self._require_context_method("tool_loop_agent")
        return await _resolve(method(**kwargs))

    def register_tools(self, *tools: Any) -> Any:
        """Register public LLM tools through ``Context.add_llm_tools``."""
        method = self._require_context_method("add_llm_tools")
        return method(*tools)

    async def send_message(self, session: str | Any, message_chain: Any) -> Any:
        """Push a message through the public session-aware sender."""
        method = self._require_context_method("send_message")
        return await _resolve(method(session, message_chain))

    async def send_text(self, session: str | Any, text: str) -> Any:
        """Build a public ``MessageChain`` text message and send it."""
        try:
            from astrbot.api.event import MessageChain
        except (ImportError, ModuleNotFoundError) as exc:
            raise UnsupportedAstrBotCapability(
                "astrbot.api.event.MessageChain is unavailable"
            ) from exc
        return await self.send_message(session, MessageChain().message(text))

    def session_origin(self, event: Any) -> str:
        """Return the complete ``unified_msg_origin`` from an event."""
        origin = getattr(event, "unified_msg_origin", None)
        if callable(origin):
            origin = origin()
        if not isinstance(origin, str) or not origin:
            raise AstrBotAdapterError("event has no valid unified_msg_origin")
        return origin

    def stop_default_agent(self, event: Any) -> None:
        """Stop default event propagation after a Pi task is accepted."""
        stopper = getattr(event, "stop_event", None)
        if not callable(stopper):
            raise UnsupportedAstrBotCapability("event.stop_event is unavailable")
        stopper()

    @property
    def supports_main_agent_wakeup(self) -> bool:
        """Whether the caller supplied an explicit wake callback.

        AstrBot's ``CronMessageEvent`` path is internal, so the default is
        false. A host integration may inject a version-specific callback and
        keep that dependency outside this adapter.
        """
        return self._wake_main_agent is not None

    async def wake_main_agent(
        self,
        session: str,
        prompt: str,
        context: Mapping[str, Any] | None = None,
    ) -> Any:
        """Invoke an explicitly injected wake callback, if one exists."""
        if self._wake_main_agent is None:
            raise UnsupportedAstrBotCapability(
                "main-agent wakeup is internal; inject a host callback explicitly"
            )
        return await _resolve(self._wake_main_agent(session, prompt, context))

    def _require_context_method(self, name: str) -> Callable[..., Any]:
        method = getattr(self.context, name, None)
        if not callable(method):
            raise UnsupportedAstrBotCapability(f"Context.{name} is unavailable")
        return method


__all__ = [
    "AstrBotAdapter",
    "AstrBotAdapterError",
    "UnsupportedAstrBotCapability",
    "WakeMainAgent",
]
