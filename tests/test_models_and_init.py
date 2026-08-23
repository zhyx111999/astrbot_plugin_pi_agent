"""Tests for pi_legacy models and package exports."""

# Shared test setup: must run before any pi_legacy import.
# isort: off
import _helpers  # noqa: F401
from pi_legacy import (  # noqa: E402
    PiConnection,
    PiConnectionManager,
    SessionInfo,
    UIRequest,
    __all__,
)
from pi_legacy.models import SessionInfo as ModelsSessionInfo  # noqa: E402
from pi_legacy.models import UIRequest as ModelsUIRequest  # noqa: E402
# isort: on


class TestSessionInfo:
    """Tests for the SessionInfo dataclass."""

    def test_required_fields(self):
        info = SessionInfo(
            session_id="abc-123",
            session_file="/tmp/session.jsonl",
            cwd="/workspace",
        )
        assert info.session_id == "abc-123"
        assert info.session_file == "/tmp/session.jsonl"
        assert info.cwd == "/workspace"

    def test_defaults(self):
        info = SessionInfo(
            session_id="abc-123",
            session_file="/tmp/session.jsonl",
            cwd="/workspace",
        )
        assert info.session_name is None
        assert info.message_count == 0
        assert info.thinking_level is None
        assert info.is_streaming is False
        assert info.timestamp is None

    def test_custom_values(self):
        info = SessionInfo(
            session_id="abc-123",
            session_file="/tmp/session.jsonl",
            cwd="/workspace",
            session_name="my-session",
            message_count=42,
            thinking_level="deep",
            is_streaming=True,
            timestamp="2026-07-03T12:00:00Z",
        )
        assert info.session_name == "my-session"
        assert info.message_count == 42
        assert info.thinking_level == "deep"
        assert info.is_streaming is True
        assert info.timestamp == "2026-07-03T12:00:00Z"


class TestUIRequest:
    """Tests for the UIRequest dataclass."""

    def test_required_fields(self):
        request = UIRequest(
            local_id=1,
            request_id="req-123",
            method="confirm",
        )
        assert request.local_id == 1
        assert request.request_id == "req-123"
        assert request.method == "confirm"

    def test_defaults(self):
        request = UIRequest(
            local_id=1,
            request_id="req-123",
            method="confirm",
        )
        assert request.title == ""
        assert request.message == ""
        assert request.options == []
        assert request.prefill == ""
        assert request.timeout_ms is None
        assert request.created_at == 0.0

    def test_to_dict(self):
        request = UIRequest(
            local_id=3,
            request_id="req-456",
            method="select",
            title="Choose one",
            message="Pick an option",
            options=["a", "b"],
            prefill="a",
            timeout_ms=1000,
            created_at=123.45,
        )
        expected = {
            "local_id": 3,
            "request_id": "req-456",
            "method": "select",
            "title": "Choose one",
            "message": "Pick an option",
            "options": ["a", "b"],
            "prefill": "a",
            "timeout_ms": 1000,
            "created_at": 123.45,
        }
        assert request.to_dict() == expected

    def test_to_dict_with_defaults(self):
        request = UIRequest(
            local_id=1,
            request_id="req-123",
            method="confirm",
        )
        expected = {
            "local_id": 1,
            "request_id": "req-123",
            "method": "confirm",
            "title": "",
            "message": "",
            "options": [],
            "prefill": "",
            "timeout_ms": None,
            "created_at": 0.0,
        }
        assert request.to_dict() == expected

    def test_options_isolation(self):
        request1 = UIRequest(local_id=1, request_id="r1", method="select")
        request2 = UIRequest(local_id=2, request_id="r2", method="select")
        request1.options.append("only")
        assert request1.options == ["only"]
        assert request2.options == []


class TestPackageExports:
    """Tests for pi_legacy.__init__ exports."""

    def test_all_exports(self):
        assert set(__all__) == {
            "PiConnection",
            "PiConnectionManager",
            "PiError",
            "SessionInfo",
            "UIRequest",
        }

    def test_session_info_is_same_class(self):
        assert SessionInfo is ModelsSessionInfo

    def test_ui_request_is_same_class(self):
        assert UIRequest is ModelsUIRequest

    def test_piconnection_is_importable(self):
        assert callable(PiConnection)

    def test_piconnectionmanager_is_importable(self):
        assert callable(PiConnectionManager)
