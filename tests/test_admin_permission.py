"""Tests for admin permission checks in PiAgentPlugin.

These tests verify that the /pi and /pic command handlers are decorated with
AstrBot's ADMIN permission filter, that the _require_admin helper behaves
correctly, and that every llm_tool method short-circuits with a permission
denial message when invoked by a non-admin user.
"""

# Shared test setup: must run before any pi_legacy import.
# isort: off
import _helpers  # noqa: F401
# isort: on

from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import FakePermissionType

import main


class TestRequireAdmin:
    """Tests for the _require_admin helper."""

    def test_returns_none_for_admin(self, plugin, admin_event):
        assert plugin._require_admin(admin_event) is None

    def test_returns_denial_for_non_admin(self, plugin, non_admin_event):
        result = plugin._require_admin(non_admin_event)
        assert result is not None
        assert "Permission denied" in result
        assert "administrators" in result


class TestLlmTools:
    """Tests for admin gating on LLM tool methods."""

    @pytest.mark.parametrize(
        "tool_name,kwargs",
        [
            ("pi_open_session", {"path": "/tmp"}),
            ("pi_list_sessions", {}),
            ("pi_resume_session", {}),
            ("pi_send_message", {"message": "hello"}),
            ("pi_get_session_info", {}),
            ("pi_run_command", {"command": "help"}),
            ("pi_get_available_commands", {}),
            ("pi_abort", {}),
            ("pi_reply_ui", {"request_id": 1, "value": "yes"}),
        ],
    )
    @pytest.mark.asyncio
    async def test_non_admin_denied_for_all_tools(self, plugin, non_admin_event, tool_name, kwargs):
        plugin.pi_connection_manager = MagicMock()
        method = getattr(plugin, tool_name)
        result = await method(non_admin_event, **kwargs)
        assert "Permission denied" in result, f"{tool_name} should deny non-admin users"
        plugin.pi_connection_manager.assert_not_called()

    @pytest.mark.asyncio
    async def test_admin_allowed_for_open_session(self, plugin, admin_event):
        plugin.pi_connection_manager.open_session = AsyncMock(
            return_value=MagicMock(
                session_id="sid",
                session_name=None,
                cwd="/tmp",
                session_file="/tmp/session.jsonl",
                message_count=0,
                thinking_level=None,
            )
        )
        result = await plugin.pi_open_session(admin_event, path="/tmp")
        plugin.pi_connection_manager.open_session.assert_awaited_once()
        assert "Opened new pi session" in result

    @pytest.mark.asyncio
    async def test_admin_allowed_for_send_message(self, plugin, admin_event, async_iter):
        fake_conn = MagicMock()
        fake_conn.send_prompt = MagicMock(return_value=async_iter([]))
        plugin._get_connection = AsyncMock(return_value=fake_conn)
        result = await plugin.pi_send_message(admin_event, message="hello")
        plugin._get_connection.assert_awaited_once()
        assert "Pi completed without a response" in result


class TestCommandHandlers:
    """Tests that command handlers require ADMIN permission."""

    def test_pi_handler_requires_admin(self):
        assert (
            main.PiAgentPlugin.pi_handler.__permission_type__
            == FakePermissionType.ADMIN
        )

    def test_pic_handler_requires_admin(self):
        assert (
            main.PiAgentPlugin.pic_handler.__permission_type__
            == FakePermissionType.ADMIN
        )
