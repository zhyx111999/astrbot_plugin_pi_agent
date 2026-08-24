"""End-to-end behavior tests for the observation-style task facade."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pi_agent_bridge.models import TaskStatus
from pi_agent_bridge.registry import TaskRegistry
from pi_agent_bridge.scheduler import TaskScheduler
from pi_agent_bridge.service import PiTaskService


class FakeAdapter:
    """Small worker double that exposes the scheduler's public adapter contract."""

    instances: list["FakeAdapter"] = []

    def __init__(self, **kwargs):
        self.task_id = kwargs["task_id"]
        self.is_running = False
        self.event_cursor = 0
        self.terminated = False
        self.steer_messages: list[str] = []
        self.cancelled = False
        self.state_calls = 0
        self.session_path = str(Path(kwargs.get("session_dir", "/tmp")) / "session.jsonl")
        self.__class__.instances.append(self)

    async def start(self):
        self.is_running = True
        return self

    async def new_session(self):
        return {"success": True}

    async def get_state(self):
        self.state_calls += 1
        return {
            "sessionId": f"session-{self.task_id}",
            "sessionPath": self.session_path,
        }

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

    async def steer(self, message):
        self.steer_messages.append(message)
        return {"success": True}

    async def cancel(self):
        self.cancelled = True
        return {"success": True}

    async def terminate(self):
        self.terminated = True
        self.is_running = False


class BlockingScheduler:
    """Scheduler double that keeps launch work queued until shutdown."""

    def __init__(self, registry):
        self.registry = registry
        self.gate = asyncio.Event()

    async def start(self):
        return None

    def workspace_for(self, task_id, requested=None):
        return requested or str(Path("/tmp") / task_id)

    async def submit(self, task, *, prompt):
        await self.gate.wait()
        return task


@pytest.mark.asyncio
async def test_shutdown_finalizes_queued_launch(tmp_path: Path):
    registry = TaskRegistry(tmp_path / "tasks.db")
    scheduler = BlockingScheduler(registry)
    service = PiTaskService(registry, scheduler)

    created = await service.create_task(owner_key="qq:1", task="queued")
    task_id = created["task_id"]
    await asyncio.sleep(0)
    await service.shutdown()

    task = registry.get_task(task_id)
    assert task.status is TaskStatus.FAILED
    assert registry.get_latest_snapshot(task_id) is None
    registry.close()


@pytest.mark.asyncio
async def test_create_returns_before_worker_and_repeated_observations_stay_running(tmp_path: Path):
    FakeAdapter.instances.clear()
    registry = TaskRegistry(tmp_path / "tasks.db")
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        adapter_factory=FakeAdapter,
        poll_interval_seconds=3600,
        no_meaningful_event_limit=3,
    )
    service = PiTaskService(registry, scheduler)

    created = await service.create_task(owner_key="qq:1", task="research")
    assert created["ok"] is True
    task_id = created["task_id"]
    assert created["status"] == TaskStatus.QUEUED.value

    await asyncio.sleep(0)
    assert registry.get_task(task_id).status is TaskStatus.RUNNING
    adapter = FakeAdapter.instances[0]

    for _ in range(3):
        await scheduler.poll_task(task_id)
    assert registry.get_task(task_id).status is TaskStatus.RUNNING
    assert adapter.terminated is False

    await service.shutdown()
    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_poll_records_a_bounded_remote_pi_state_snapshot(tmp_path: Path):
    FakeAdapter.instances.clear()
    registry = TaskRegistry(tmp_path / "tasks.db")
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        adapter_factory=FakeAdapter,
        poll_interval_seconds=3600,
    )
    service = PiTaskService(registry, scheduler)

    created = await service.create_task(owner_key="qq:1", task="inspect")
    task_id = created["task_id"]
    await asyncio.sleep(0)
    await scheduler.poll_task(task_id)
    assert registry.get_latest_snapshot(task_id) is None

    await service.shutdown()
    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_main_model_poll_explicitly_checks_worker_without_returning_events(tmp_path: Path):
    FakeAdapter.instances.clear()
    registry = TaskRegistry(tmp_path / "tasks.db")
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        adapter_factory=FakeAdapter,
        poll_interval_seconds=3600,
    )
    service = PiTaskService(registry, scheduler)

    created = await service.create_task(owner_key="qq:1", task="inspect")
    task_id = created["task_id"]
    await asyncio.sleep(0)
    adapter = FakeAdapter.instances[0]
    calls_after_start = adapter.state_calls
    snapshot_before = registry.get_latest_snapshot(task_id)

    result = await service.poll(task_id)

    assert result["ok"] is True
    assert adapter.state_calls == calls_after_start + 1
    assert registry.get_latest_snapshot(task_id) == snapshot_before
    assert registry.get_task(task_id).event_cursor == "0"
    assert result["content"] == []

    await service.shutdown()
    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_repeated_main_model_polls_do_not_overwrite_observer_progress(tmp_path: Path):
    FakeAdapter.instances.clear()
    registry = TaskRegistry(tmp_path / "tasks.db")
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        adapter_factory=FakeAdapter,
        poll_interval_seconds=3600,
    )
    service = PiTaskService(registry, scheduler)

    created = await service.create_task(owner_key="qq:1", task="inspect")
    task_id = created["task_id"]
    await asyncio.sleep(0)
    adapter = FakeAdapter.instances[0]
    adapter.event_cursor = 1

    class Event:
        meaningful = True
        payload = {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": "progress",
            },
        }

        def as_dict(self):
            return {
                "cursor": 1,
                "meaningful": True,
                "payload": self.payload,
            }

    adapter.drain_events = lambda *, after_cursor=0, **_kwargs: [Event()]
    session_path = Path(adapter.session_path)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_bytes('{"type":"message","text":"progress"}\n'.encode())

    await scheduler.poll_task(task_id)
    observed = registry.get_latest_snapshot(task_id)
    assert observed is None

    first = await service.poll(task_id)
    second = await service.poll(task_id)

    assert first["content"] == []
    assert second["content"] == []
    assert service.read(task_id, cursor=0, limit=10)["session_lines"] == [
        '{"type":"message","text":"progress"}\n'
    ]

    await service.shutdown()
    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_result_reads_native_pi_session_lines_by_cursor(tmp_path: Path):
    FakeAdapter.instances.clear()
    registry = TaskRegistry(tmp_path / "tasks.db")
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        adapter_factory=FakeAdapter,
        poll_interval_seconds=3600,
    )
    service = PiTaskService(registry, scheduler)
    created = await service.create_task(owner_key="qq:1", task="inspect")
    task_id = created["task_id"]
    await asyncio.sleep(0)
    adapter = FakeAdapter.instances[-1]
    session_path = Path(adapter.session_path)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    raw_line = '{"type":"message_update","delta":"' + ("x" * 5000) + '"}\n'
    session_path.write_bytes(raw_line.encode())

    recent = service.read(task_id)
    assert len(recent["session_text"]) <= 50_000
    assert recent["progress"]["read"]["mode"] == "recent_tail"

    result = service.result(task_id, offset=0, limit=1)

    assert result["content"] == []
    assert result["session_lines"] == [raw_line]
    assert result["progress"]["read"] == {
        "mode": "full_lines",
        "cursor": 0,
        "next_cursor": 1,
        "returned": 1,
        "has_more": False,
        "source": "pi_native_session_jsonl",
        "session_path": str(session_path),
    }

    await service.shutdown()
    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_provider_error_remains_in_native_session(tmp_path: Path):
    FakeAdapter.instances.clear()
    registry = TaskRegistry(tmp_path / "provider-error-tasks.db")
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        adapter_factory=FakeAdapter,
        poll_interval_seconds=3600,
    )
    service = PiTaskService(registry, scheduler)
    created = await service.create_task(owner_key="qq:1", task="inspect")
    task_id = created["task_id"]
    await asyncio.sleep(0)
    adapter = FakeAdapter.instances[-1]
    session_path = Path(adapter.session_path)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    raw_line = '{"type":"message_end","message":{"stopReason":"error","errorMessage":"OpenAI API error (502): upstream unavailable"}}\n'
    session_path.write_bytes(raw_line.encode())

    result = service.result(task_id)

    assert registry.get_task(task_id).status is TaskStatus.RUNNING
    assert result["error"] is None
    assert result["session_text"] == raw_line
    assert result["progress"]["read"]["mode"] == "recent_tail"

    await service.shutdown()
    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_status_repeated_reads_do_not_repeat_snapshot_content(tmp_path: Path):
    registry = TaskRegistry(tmp_path / "status-tasks.db")
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        adapter_factory=FakeAdapter,
        poll_interval_seconds=3600,
    )
    service = PiTaskService(registry, scheduler)
    created = await service.create_task(owner_key="qq:1", task="inspect")
    task_id = created["task_id"]
    await asyncio.sleep(0)

    class Event:
        meaningful = True
        payload = {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "once"},
        }

        def as_dict(self):
            return {"cursor": 1, "meaningful": True, "payload": self.payload}

    adapter = FakeAdapter.instances[-1]
    session_path = Path(adapter.session_path)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_bytes('{"type":"message_update","delta":"once"}\n'.encode())

    await scheduler.poll_task(task_id)

    status = service.status(task_id)
    repeated_status = service.status(task_id)
    first_poll = await service.poll(task_id)
    repeated_poll = await service.poll(task_id)

    assert status["content"] == []
    assert repeated_status["content"] == []
    assert "snapshot" not in status["progress"]
    assert "snapshot" not in repeated_status["progress"]
    assert first_poll["content"] == []
    assert repeated_poll["content"] == []
    assert service.read(task_id, cursor=0, limit=10)["session_lines"] == [
        '{"type":"message_update","delta":"once"}\n'
    ]

    await service.shutdown()
    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_terminal_observation_never_notifies_chat(tmp_path: Path):
    FakeAdapter.instances.clear()
    registry = TaskRegistry(tmp_path / "tasks.db")
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        adapter_factory=FakeAdapter,
        poll_interval_seconds=3600,
        no_meaningful_event_limit=1,
    )
    service = PiTaskService(registry, scheduler)
    created = await service.create_task(owner_key="qq:1", task="inspect")
    task_id = created["task_id"]
    await asyncio.sleep(0)

    await scheduler.poll_task(task_id)
    await scheduler.poll_task(task_id)

    assert registry.get_task(task_id).status is TaskStatus.RUNNING

    await service.shutdown()
    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_resume_steers_same_worker_and_cancel_delete_cleanup(tmp_path: Path):
    FakeAdapter.instances.clear()
    registry = TaskRegistry(tmp_path / "tasks.db")
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        adapter_factory=FakeAdapter,
        poll_interval_seconds=3600,
        no_meaningful_event_limit=1,
    )
    service = PiTaskService(registry, scheduler)

    created = await service.create_task(owner_key="qq:1", task="code")
    task_id = created["task_id"]
    await asyncio.sleep(0)
    await scheduler.poll_task(task_id)
    adapter = FakeAdapter.instances[0]
    assert registry.get_task(task_id).status is TaskStatus.RUNNING

    resumed = await service.resume(task_id)
    assert resumed["ok"] is True
    assert adapter.terminated is False
    assert adapter.steer_messages

    cancelled = await service.cancel(task_id)
    assert cancelled["status"] == TaskStatus.CANCELLED.value
    assert adapter.cancelled is True

    deleted = await service.delete(task_id)
    assert deleted["ok"] is True
    assert registry.list_tasks() == []
    assert adapter.terminated is True

    await service.shutdown()
    await scheduler.shutdown()
    registry.close()
