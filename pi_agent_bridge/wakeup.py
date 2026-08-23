"""Latest-only wakeup queue for optional host integrations."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any


class WakeupAdapter:
    """Queue task updates without sending raw Pi output directly to users."""

    def __init__(self) -> None:
        self._latest: dict[str, dict[str, Any]] = {}
        self._event = asyncio.Event()

    def publish(self, task_id: str, envelope: Mapping[str, Any]) -> None:
        self._latest[task_id] = dict(envelope)
        self._event.set()

    def get(self, task_id: str) -> dict[str, Any] | None:
        value = self._latest.get(task_id)
        return dict(value) if value is not None else None

    def pop(self, task_id: str) -> dict[str, Any] | None:
        value = self._latest.pop(task_id, None)
        if not self._latest:
            self._event.clear()
        return dict(value) if value is not None else None

    def list_pending(self) -> list[dict[str, Any]]:
        return [dict(value) for value in self._latest.values()]

    async def wait(self, timeout: float | None = None) -> bool:
        try:
            if timeout is None:
                await self._event.wait()
            else:
                await asyncio.wait_for(self._event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

