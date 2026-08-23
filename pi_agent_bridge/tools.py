"""Operation registry used by the plugin's LLM tool facade."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


class ToolRegistry:
    """Keep the public operation list in one inspectable module."""

    def __init__(self) -> None:
        self._operations: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, handler: Callable[..., Any]) -> None:
        if not name or not callable(handler):
            raise ValueError("tool name and handler are required")
        self._operations[name] = handler

    def get(self, name: str) -> Callable[..., Any] | None:
        return self._operations.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._operations)

    async def invoke(self, name: str, *args: Any, **kwargs: Any) -> Any:
        handler = self.get(name)
        if handler is None:
            raise KeyError(f"unknown Pi task operation: {name}")
        result = handler(*args, **kwargs)
        if isinstance(result, Awaitable):
            return await result
        return result

