"""Regression tests for the main-plugin task bridge integration."""

# isort: off
import _helpers  # noqa: F401
# isort: on

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
from pi_agent_bridge.models import TaskStatus
from pi_agent_bridge.registry import TaskRegistry
from pi_agent_bridge.worker import WORKER_DESCRIPTOR_KEY


def _event(origin: str, *, admin: bool = False):
    return SimpleNamespace(
        unified_msg_origin=origin,
        message_str="",
        is_admin=lambda: admin,
    )


def test_terminal_wakeup_requires_interpreted_user_reply():
    note = main._terminal_wakeup_note(
        "task-1",
        "completed",
        "worker_finished",
        "native tail",
    )

    assert "Pi 会话原文" in note
    assert "JSONL" in note
    assert "send_message_to_user" in note
    assert "native tail" in note
    assert "interpreted_user_reply_only" not in note


def test_task_permission_default_matches_schema(plugin, non_admin_event):
    """Background tasks default to owner-scoped access, not admin-only."""
    assert plugin._require_task_permission(non_admin_event) is None


def test_config_bool_accepts_serialized_values(plugin):
    plugin.plugin_config = {"enable_async_tasks": "false"}
    assert plugin._config_bool("enable_async_tasks", True) is False
    plugin.plugin_config = {"enable_async_tasks": "TRUE"}
    assert plugin._config_bool("enable_async_tasks", False) is True


@pytest.mark.asyncio
async def test_task_status_initializes_registry_before_visibility_check(plugin, tmp_path):
    owner_event = _event("qq:owner")
    other_event = _event("qq:other")
    registry = TaskRegistry(tmp_path / "tasks.db")
    task = registry.create_task(owner_key="qq:owner", prompt="long task", status=TaskStatus.QUEUED)
    plugin._task_registry = registry
    plugin.pi_task_service = MagicMock()
    plugin.pi_task_service.status.return_value = {
        "schema_version": "1",
        "ok": True,
        "operation": "task_status",
        "task_id": task.task_id,
        "status": "queued",
    }

    visible = await plugin.pi_task_status(owner_event, task.task_id)
    assert '"ok":true' in visible
    plugin.pi_task_service.status.assert_called_once_with(task.task_id)

    readable = await plugin.pi_task_status(other_event, task.task_id)
    assert '"ok":true' in readable
    assert plugin.pi_task_service.status.call_count == 2
    registry.close()


@pytest.mark.asyncio
async def test_disabled_bridge_returns_structured_error_without_registry(plugin):
    plugin.plugin_config = {"enable_async_tasks": "false"}
    result = await plugin.pi_task_status(_event("qq:owner"), "missing")
    assert '"ok":false' in result
    assert "disabled" in result
    assert plugin.pi_task_service is None
    assert plugin._task_registry is None


@pytest.mark.asyncio
async def test_create_service_applies_retention_configuration(plugin, tmp_path):
    plugin.plugin_config = {
        "state_directory": str(tmp_path / "state"),
        "session_retention_hours": 72,
    }
    service = await plugin._ensure_task_service()
    assert service.scheduler.session_retention_hours == 72
    assert service.scheduler.session_root == (tmp_path / "state" / "sessions").resolve()
    plugin.pi_connection_manager.terminate_all = AsyncMock()
    await plugin.terminate()


@pytest.mark.asyncio
async def test_invalid_skill_path_does_not_block_plugin_service_creation(plugin, tmp_path):
    plugin.plugin_config = {
        "state_directory": str(tmp_path / "state"),
        "pi_skill_paths": [str(tmp_path / "missing-skill")],
    }

    service = await plugin._ensure_task_service()

    assert service.scheduler.worker_config_factory is not None
    plugin.pi_connection_manager.terminate_all = AsyncMock()
    await plugin.terminate()


@pytest.mark.asyncio
async def test_pi_agent_sends_only_refined_request_and_descriptor(plugin):
    plugin.plugin_config = {
        "pi_model": "fixed-provider/fixed-model",
        "pi_thinking_level": "high",
    }
    service = MagicMock()
    service.create_task = AsyncMock(return_value={"ok": True, "status": "queued"})
    plugin.pi_task_service = service
    await plugin.pi_agent(_event("qq:owner"), "整理这个请求并执行它")

    kwargs = service.create_task.await_args.kwargs
    assert kwargs["task"] == "整理这个请求并执行它"
    assert "persona" not in kwargs
    assert "media_references" not in kwargs
    assert set(kwargs["context"]) == {WORKER_DESCRIPTOR_KEY}
    assert kwargs["context"][WORKER_DESCRIPTOR_KEY]["model_settings"]["thinking_level"] == "high"


@pytest.mark.asyncio
async def test_pi_agent_uses_only_fixed_provider_and_model_descriptor(plugin):
    class Context:
        async def get_current_chat_provider_id(self, _umo):
            raise AssertionError("pi_agent must not inherit the current chat provider")

    plugin.plugin_config = {"pi_model": "gateway/provider-model"}
    plugin.astrbot_adapter.context = Context()
    service = MagicMock()
    service.create_task = AsyncMock(return_value={"ok": True, "status": "queued"})
    plugin.pi_task_service = service

    result = json.loads(await plugin.pi_agent(_event("qq:owner"), "research"))

    assert result == {"ok": True, "status": "queued"}
    task_context = service.create_task.await_args.kwargs["context"]
    assert task_context[WORKER_DESCRIPTOR_KEY]["source_provider_id"] == "gateway/provider-model"
    assert task_context[WORKER_DESCRIPTOR_KEY]["model_settings"]["thinking_level"] == "max"
    assert "api_key" not in json.dumps(task_context).lower()


@pytest.mark.asyncio
async def test_pi_agent_rejects_mcp_config_with_structured_envelope(plugin):
    plugin.plugin_config = {
        "pi_model": "fixed-provider/fixed-model",
        "pi_mcp_config_paths": ["/configured/mcp.json"],
    }
    service = MagicMock()
    service.create_task = AsyncMock()
    plugin.pi_task_service = service

    result = json.loads(await plugin.pi_agent(_event("qq:owner"), "research"))

    assert result["schema_version"] == "1"
    assert result["ok"] is False
    assert result["operation"] == "task_create"
    assert result["task_id"] is None
    assert result["status"] is None
    assert result["progress"] == {}
    assert result["content"] == []
    assert result["artifacts"] == []
    assert result["error"]["type"] == "pi_task_error"
    assert "MCP integration is unsupported" in result["error"]["message"]
    service.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminate_closes_service_before_scheduler(plugin):
    service = MagicMock()
    service.shutdown = MagicMock()
    scheduler = MagicMock()
    scheduler.shutdown = MagicMock()
    plugin.pi_task_service = service
    plugin.pi_task_scheduler = scheduler
    plugin.pi_connection_manager.terminate_all = AsyncMock()

    # Async mocks are intentionally supplied as coroutine-returning methods.
    async def service_shutdown():
        service.shutdown_called = True

    async def scheduler_shutdown():
        scheduler.shutdown_called = True

    service.shutdown = service_shutdown
    scheduler.shutdown = scheduler_shutdown
    await plugin.terminate()
    assert service.shutdown_called is True
    assert scheduler.shutdown_called is True
    assert plugin.pi_task_service is None
    assert plugin.pi_task_scheduler is None
