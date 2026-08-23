"""Runtime selection is lazy and remains outside the AstrBot tool turn."""

from __future__ import annotations

from pathlib import Path

import pytest

from pi_agent_bridge.models import TaskStatus
from pi_agent_bridge.registry import TaskRegistry
from pi_agent_bridge.scheduler import TaskScheduler


class FakeRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def resolve_command(self) -> tuple[str, ...]:
        self.calls += 1
        return ("/plugin/node", "/plugin/pi-cli.js")


class FakeWorker:
    created: list["FakeWorker"] = []

    def __init__(self, **kwargs):
        self.task_id = kwargs["task_id"]
        self.executable = kwargs["executable"]
        self.is_running = False
        self.event_cursor = 0
        self.process = None
        self.__class__.created.append(self)

    async def start(self):
        self.is_running = True
        return self

    async def new_session(self):
        return {"success": True}

    async def get_state(self):
        return {"sessionId": f"session-{self.task_id}"}

    async def send_prompt_nowait(self, _prompt):
        return "prompt-id"

    def drain_events(self, *, after_cursor=0, **_kwargs):
        return []

    def snapshot(self):
        return {
            "task_id": self.task_id,
            "state": "running" if self.is_running else "terminated",
            "running": self.is_running,
            "returncode": None,
            "event_cursor": self.event_cursor,
        }

    async def terminate(self):
        self.is_running = False


@pytest.mark.asyncio
async def test_runtime_resolution_is_lazy_and_passed_as_argv(tmp_path: Path):
    FakeWorker.created.clear()
    runtime = FakeRuntime()
    registry = TaskRegistry(tmp_path / "tasks.db")
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        adapter_factory=FakeWorker,
        runtime_adapter=runtime,
        poll_interval_seconds=3600,
        session_retention_hours=None,
    )

    await scheduler.start()
    assert runtime.calls == 0

    task = registry.create_task(owner_key="owner", prompt="long task")
    await scheduler.submit(task, prompt="long task")

    assert runtime.calls == 1
    assert FakeWorker.created[0].executable == (
        "/plugin/node",
        "/plugin/pi-cli.js",
    )
    assert registry.get_task(task.task_id).status is TaskStatus.RUNNING

    await scheduler.shutdown()
    registry.close()

