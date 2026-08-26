"""Tests for the fresh task registry schema."""

from __future__ import annotations

import pytest

from pi_agent_bridge import TaskRegistry, TaskStatus
from pi_agent_bridge.registry import InvalidTaskTransition, TaskNotFoundError


def test_task_lifecycle_and_runtime_metadata(tmp_path):
    with TaskRegistry(tmp_path / "tasks_v4.db") as registry:
        task = registry.create_task(
            owner_key="snowluma:3268514224",
            session_origin="snowluma:GroupMessage:748796098",
            prompt="research",
        )
        assert task.status is TaskStatus.QUEUED
        running = registry.transition_status(task.task_id, TaskStatus.RUNNING)
        assert running.status is TaskStatus.RUNNING
        updated = registry.update_runtime(
            task.task_id,
            session_id="pi-session",
            session_path="/tmp/session.jsonl",
            process_id=123,
            event_cursor="42",
        )
        assert updated.session_id == "pi-session"
        assert updated.event_cursor == "42"
        assert registry.get_task(task.task_id).session_origin == (
            "snowluma:GroupMessage:748796098"
        )


def test_task_database_has_only_current_schema(tmp_path):
    with TaskRegistry(tmp_path / "tasks_v4.db") as registry:
        columns = {
            row["name"]
            for row in registry._connection.execute("pragma table_info(tasks)")
        }
        assert columns == {
            "task_id",
            "owner_key",
            "session_origin",
            "status",
            "prompt",
            "context_json",
            "session_id",
            "session_path",
            "process_id",
            "workspace",
            "event_cursor",
            "created_at",
            "updated_at",
            "finished_at",
        }
        tables = {
            row[0]
            for row in registry._connection.execute(
                "select name from sqlite_master where type='table'"
            )
        }
        assert tables == {"tasks"}


def test_task_delete_and_missing_task(tmp_path):
    with TaskRegistry(tmp_path / "tasks_v4.db") as registry:
        task = registry.create_task(
            owner_key="owner",
            session_origin="platform:FriendMessage:user",
            prompt="x",
        )
        registry.delete_task(task.task_id)
        assert registry.list_tasks() == []
        with pytest.raises(TaskNotFoundError):
            registry.get_task(task.task_id)


def test_retention_only_deletes_expired_terminal_tasks(tmp_path):
    with TaskRegistry(tmp_path / "tasks_v4.db") as registry:
        old = registry.create_task(
            owner_key="owner",
            session_origin="platform:FriendMessage:user",
            prompt="old",
        )
        registry.transition_status(old.task_id, TaskStatus.RUNNING)
        registry.transition_status(old.task_id, TaskStatus.COMPLETED)
        active = registry.create_task(
            owner_key="owner",
            session_origin="platform:FriendMessage:user",
            prompt="active",
        )
        registry.transition_status(active.task_id, TaskStatus.RUNNING)
        removed = registry.purge_expired_tasks(0)
        assert [item.task_id for item in removed] == [old.task_id]
        assert registry.get_task(active.task_id).status is TaskStatus.RUNNING


def test_invalid_transition_is_rejected(tmp_path):
    with TaskRegistry(tmp_path / "tasks_v4.db") as registry:
        task = registry.create_task(
            owner_key="owner",
            session_origin="platform:FriendMessage:user",
            prompt="x",
        )
        registry.transition_status(task.task_id, TaskStatus.RUNNING)
        registry.transition_status(task.task_id, TaskStatus.COMPLETED)
        with pytest.raises(InvalidTaskTransition):
            registry.transition_status(task.task_id, TaskStatus.RUNNING)
