"""Tests for pi_legacy/connection.py (mocked sync/async parts)."""

import asyncio

# Shared test setup: must run before any pi_legacy import.
# isort: off
import _helpers  # noqa: F401
from pi_legacy.connection import PiConnection  # noqa: E402
from pi_legacy.models import UIRequest  # noqa: E402
# isort: on

import pytest


class TestPiConnectionId:
    """Tests for the small synchronous ID generators."""

    def test_next_command_id(self):
        conn = PiConnection()
        id1 = conn._next_command_id()
        id2 = conn._next_command_id()
        assert id1 != id2
        assert id1.startswith("pi-cmd-1-")
        assert id2.startswith("pi-cmd-2-")

    def test_next_ui_local_id(self):
        conn = PiConnection()
        assert conn._next_ui_local_id() == 1
        assert conn._next_ui_local_id() == 2
        assert conn._next_ui_local_id() == 3


class TestPiConnectionEvents:
    """Tests for event handling and normalization without a real pi process."""

    @pytest.fixture
    def conn(self):
        return PiConnection()

    @pytest.mark.asyncio
    async def test_handle_event_response(self, conn):
        future = asyncio.get_running_loop().create_future()
        conn._pending_responses["resp-1"] = future
        event = {"type": "response", "id": "resp-1", "data": "ok"}
        await conn._handle_event(event)
        assert future.done()
        assert future.result() == event
        assert "resp-1" not in conn._pending_responses

    @pytest.mark.asyncio
    async def test_handle_event_extension_ui_request(self, conn):
        event = {
            "type": "extension_ui_request",
            "id": "req-1",
            "method": "confirm",
            "title": "Confirm?",
            "message": "Are you sure?",
        }
        await conn._handle_event(event)
        assert "req-1" in conn.pending_ui_requests
        req = conn.pending_ui_requests["req-1"]
        assert req.local_id == 1
        assert req.method == "confirm"
        assert req.title == "Confirm?"

    @pytest.mark.asyncio
    async def test_track_ui_request_ignores_non_interactive(self, conn):
        event = {
            "type": "extension_ui_request",
            "id": "req-2",
            "method": "setStatus",
        }
        conn._track_ui_request(event)
        assert "req-2" not in conn.pending_ui_requests

    @pytest.mark.asyncio
    async def test_get_ui_request_by_local_id(self, conn):
        event = {
            "type": "extension_ui_request",
            "id": "req-3",
            "method": "select",
            "options": ["a", "b"],
        }
        await conn._handle_event(event)
        req = conn.get_ui_request_by_local_id(1)
        assert req is not None
        assert req.request_id == "req-3"
        assert conn.get_ui_request_by_local_id(999) is None

    @pytest.mark.asyncio
    async def test_normalize_event_text_delta(self, conn):
        event = {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "hello"},
        }
        result = conn._normalize_event(event)
        assert result == {"type": "text", "text": "hello"}

    @pytest.mark.asyncio
    async def test_normalize_event_thinking_delta(self, conn):
        event = {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "thinking_delta",
                "delta": "thinking",
            },
        }
        result = conn._normalize_event(event)
        assert result == {"type": "thinking", "text": "thinking"}

    @pytest.mark.asyncio
    async def test_normalize_event_ui_request(self, conn):
        conn.pending_ui_requests["req-4"] = UIRequest(
            local_id=7,
            request_id="req-4",
            method="input",
        )
        event = {
            "type": "extension_ui_request",
            "id": "req-4",
        }
        result = conn._normalize_event(event)
        assert result["type"] == "ui_request"
        assert result["request"].local_id == 7
        assert result["request"].method == "input"

    @pytest.mark.asyncio
    async def test_normalize_event_fallback(self, conn):
        event = {"type": "unknown", "data": "x"}
        result = conn._normalize_event(event)
        assert result == {"type": "event", "event": event}

    @pytest.mark.asyncio
    async def test_handle_event_puts_non_response_on_queue(self, conn):
        event = {"type": "agent_end"}
        await conn._handle_event(event)
        queued = conn._event_queue.get_nowait()
        assert queued == event
