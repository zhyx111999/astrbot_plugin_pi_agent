"""Persistence cleanup and restart recovery tests for the Pi bridge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pi_agent_bridge.models import TaskStatus
from pi_agent_bridge.registry import TaskRegistry
from pi_agent_bridge.scheduler import TaskScheduler
from pi_agent_bridge.worker import PiWorkerConfig


class RecoveryAdapter:
    """Minimal reconnectable worker used by scheduler lifecycle tests."""

    def __init__(self, **kwargs):
        self.task_id = kwargs["task_id"]
        self.kwargs = kwargs
        self.process = SimpleNamespace(pid=42001, returncode=None)
        self.is_running = False
        self.terminated = False
        self.steer_messages: list[str] = []
        self.event_cursor = 0

    async def start(self):
        self.is_running = True
        self.process.returncode = None
        return self

    async def new_session(self):
        return {"success": True}

    async def get_state(self):
        return {
            "sessionId": f"session-{self.task_id}",
            "sessionPath": self.kwargs.get("session_path"),
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
            "returncode": self.process.returncode,
            "event_cursor": 0,
        }

    async def steer(self, message):
        self.steer_messages.append(message)
        return {"success": True}

    async def cancel(self):
        return {"success": True}

    async def terminate(self):
        self.terminated = True
        self.is_running = False
        self.process.returncode = 0


class RecoveryFactory:
    def __init__(self):
        self.created: list[RecoveryAdapter] = []

    def __call__(self, **kwargs):
        adapter = RecoveryAdapter(**kwargs)
        self.created.append(adapter)
        return adapter


def _running_task(registry: TaskRegistry, *, task_id: str = "task-1", **kwargs):
    task = registry.create_task(
        owner_key="owner",
        session_origin="snowluma:GroupMessage:748796098",
        prompt="long task",
        task_id=task_id,
        **kwargs,
    )
    return registry.transition_status(task.task_id, TaskStatus.RUNNING)


def _task_worker_config(task):
    config = task.context["worker_config"]
    return PiWorkerConfig(
        provider=config["provider"],
        model=config["model"],
        # Credentials are rehydrated by the host-side factory, never read
        # from the durable task context.
        environment={"PI_TASK_KEY": f"key-for-{config['provider']}"},
        skill_paths=tuple(config["skill_paths"]),
        extension_paths=tuple(config["extension_paths"]),
    )


def _worker_context(*, provider: str, model: str):
    return {
        "worker_config": {
            "provider": provider,
            "model": model,
            "skill_paths": [f"/skills/{provider}"],
            "extension_paths": [f"/extensions/{provider}"],
        }
    }


def _assert_task_config(kwargs, *, task_id: str, agent_root: Path, provider: str, model: str):
    assert kwargs["provider"] == provider
    assert kwargs["model"] == model
    assert kwargs["environment"] == {"PI_TASK_KEY": f"key-for-{provider}"}
    assert kwargs["skill_paths"] == (f"/skills/{provider}",)
    assert kwargs["extension_paths"] == (f"/extensions/{provider}",)
    assert kwargs["agent_dir"] == str((agent_root / task_id).resolve())


def test_registry_retention_deletes_only_expired_terminal_rows(tmp_path: Path):
    database = tmp_path / "tasks_v4.db"
    with TaskRegistry(database) as registry:
        old = _running_task(registry, task_id="old")
        registry.transition_status(old.task_id, TaskStatus.COMPLETED)
        recent = _running_task(registry, task_id="recent")
        registry.transition_status(recent.task_id, TaskStatus.COMPLETED)
        active = _running_task(registry, task_id="active")

        future = datetime.now(timezone.utc) + timedelta(hours=2)
        removed = registry.purge_expired_tasks(1, now=future)

        assert [item.task_id for item in removed] == ["old", "recent"]
        assert registry.get_task(active.task_id).status is TaskStatus.RUNNING
        assert registry.list_tasks() == [registry.get_task(active.task_id)]


def test_registry_persists_session_path(tmp_path: Path):
    database = tmp_path / "tasks_v4.db"
    with TaskRegistry(database) as registry:
        task = registry.create_task(
            owner_key="owner",
            session_origin="snowluma:FriendMessage:3268514224",
            prompt="x",
            session_path=str(tmp_path / "session.jsonl"),
        )
        assert task.session_path.endswith("session.jsonl")

    with TaskRegistry(database) as reopened:
        assert reopened.get_task(task.task_id).session_path.endswith("session.jsonl")


@pytest.mark.asyncio
async def test_restart_resumes_dead_worker_from_native_session(tmp_path: Path):
    registry = TaskRegistry(tmp_path / "tasks_v4.db")
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","id":"sid"}\n', encoding="utf-8")
    task = _running_task(
        registry,
        session_path=str(session),
        process_id=999999,
        workspace=str(tmp_path / "workspace"),
    )
    factory = RecoveryFactory()
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        adapter_factory=factory,
        process_probe=lambda _pid: False,
        poll_interval_seconds=3600,
        session_retention_hours=None,
    )

    await scheduler.start()
    resumed = registry.get_task(task.task_id)
    assert resumed.status is TaskStatus.RUNNING
    assert resumed.process_id == 42001
    assert factory.created[-1].steer_messages

    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_restart_keeps_logically_paused_task_paused(tmp_path: Path):
    registry = TaskRegistry(tmp_path / "tasks_v4.db")
    task = _running_task(
        registry,
        session_path=str(tmp_path / "missing-session.jsonl"),
        process_id=999999,
    )
    assert registry.get_task(task.task_id).status is TaskStatus.RUNNING
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        process_probe=lambda _pid: False,
        poll_interval_seconds=3600,
        session_retention_hours=None,
    )

    await scheduler.start()
    assert registry.get_task(task.task_id).status is TaskStatus.ORPHANED
    await scheduler.poll_task(task.task_id)
    assert registry.get_task(task.task_id).status is TaskStatus.ORPHANED

    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_submit_passes_task_scoped_worker_config_to_new_worker(tmp_path: Path):
    registry = TaskRegistry(tmp_path / "tasks_v4.db")
    task = registry.create_task(
        owner_key="owner",
        session_origin="snowluma:FriendMessage:3268514224",
        prompt="long task",
        context=_worker_context(provider="submit-provider", model="submit-model"),
    )
    factory = RecoveryFactory()
    agent_root = tmp_path / "agents"
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        agent_root=agent_root,
        adapter_factory=factory,
        worker_config_factory=_task_worker_config,
        poll_interval_seconds=3600,
        session_retention_hours=None,
    )

    await scheduler.submit(task, prompt="long task")

    _assert_task_config(
        factory.created[0].kwargs,
        task_id=task.task_id,
        agent_root=agent_root,
        provider="submit-provider",
        model="submit-model",
    )
    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_explicit_resume_restarts_orphaned_session(tmp_path: Path):
    registry = TaskRegistry(tmp_path / "tasks_v4.db")
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","id":"sid"}\n', encoding="utf-8")
    task = registry.create_task(
        owner_key="owner",
        session_origin="snowluma:FriendMessage:3268514224",
        prompt="long task",
        session_path=str(session),
        workspace=str(tmp_path / "workspace"),
        status=TaskStatus.ORPHANED,
    )
    factory = RecoveryFactory()
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        adapter_factory=factory,
        process_probe=lambda _pid: False,
        poll_interval_seconds=3600,
        session_retention_hours=None,
    )

    await scheduler.start()
    resumed = await scheduler.resume(task.task_id)
    assert resumed.status is TaskStatus.RUNNING
    assert factory.created[0].kwargs["session_path"] == str(session)
    assert factory.created[0].steer_messages

    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_resume_rebuilds_task_scoped_worker_config(tmp_path: Path):
    registry = TaskRegistry(tmp_path / "tasks_v4.db")
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","id":"sid"}\n', encoding="utf-8")
    task = registry.create_task(
        owner_key="owner",
        session_origin="snowluma:FriendMessage:3268514224",
        prompt="long task",
        status=TaskStatus.ORPHANED,
        session_path=str(session),
        context=_worker_context(provider="resume-provider", model="resume-model"),
    )
    factory = RecoveryFactory()
    agent_root = tmp_path / "agents"
    scheduler = TaskScheduler(
        registry,
        workspace_root=tmp_path / "workspaces",
        agent_root=agent_root,
        adapter_factory=factory,
        worker_config_factory=_task_worker_config,
        process_probe=lambda _pid: False,
        poll_interval_seconds=3600,
        session_retention_hours=None,
    )

    await scheduler.start()
    await scheduler.resume(task.task_id)

    _assert_task_config(
        factory.created[0].kwargs,
        task_id=task.task_id,
        agent_root=agent_root,
        provider="resume-provider",
        model="resume-model",
    )
    await scheduler.shutdown()
    registry.close()


@pytest.mark.asyncio
async def test_scheduler_cleanup_removes_owned_workspace_and_sessions(tmp_path: Path):
    registry = TaskRegistry(tmp_path / "tasks_v4.db")
    task = _running_task(registry, task_id="expired")
    registry.transition_status(task.task_id, TaskStatus.COMPLETED)
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / task.task_id
    registry.update_runtime(task.task_id, workspace=str(workspace))
    workspace.mkdir(parents=True)
    (workspace / "result.md").write_text("done", encoding="utf-8")
    session_root = tmp_path / "sessions"
    session_dir = session_root / task.task_id
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    scheduler = TaskScheduler(
        registry,
        workspace_root=workspace_root,
        session_root=session_root,
        adapter_factory=RecoveryAdapter,
        poll_interval_seconds=3600,
        session_retention_hours=None,
    )
    removed = scheduler.cleanup_expired_tasks(0)
    assert [item.task_id for item in removed] == [task.task_id]
    assert not workspace.exists()
    assert not session_dir.exists()
    assert registry.list_tasks() == []

    await scheduler.shutdown()
    registry.close()
