"""Tests for pi_connector/manager.py."""

import json
import os
import time
import types

# Shared test setup: must run before any pi_connector import.
# isort: off
import _helpers  # noqa: F401
from pi_connector.connection import PiError  # noqa: E402
from pi_connector.manager import PiConnectionManager  # noqa: E402
# isort: on

import pytest


class TestSessionKey:
    """Tests for PiConnectionManager._session_key."""

    def test_session_key_with_all_attrs(self):
        event = types.SimpleNamespace(
            get_platform_name=lambda: "qq",
            get_group_id=lambda: "123",
            get_sender_id=lambda: "456",
        )
        mgr = PiConnectionManager()
        assert mgr._session_key(event) == "qq:123:456"

    def test_session_key_missing_attrs(self):
        event = object()
        mgr = PiConnectionManager()
        assert mgr._session_key(event) == "unknown::"

    def test_session_key_partial(self):
        event = types.SimpleNamespace(get_platform_name=lambda: "telegram")
        mgr = PiConnectionManager()
        assert mgr._session_key(event) == "telegram::"


class TestNormalizePath:
    """Tests for PiConnectionManager._normalize_path."""

    def test_relative_path_resolves_to_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        mgr = PiConnectionManager()
        assert mgr._normalize_path("code/project") == os.path.join(
            "/home/testuser", "code/project"
        )

    def test_tilde_path_expands(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        mgr = PiConnectionManager()
        assert mgr._normalize_path("~/code/project") == "/home/testuser/code/project"

    def test_absolute_path_unchanged(self):
        mgr = PiConnectionManager()
        assert mgr._normalize_path("/opt/project") == "/opt/project"

    def test_trailing_slash_removed(self):
        mgr = PiConnectionManager()
        assert mgr._normalize_path("/home/user/project/") == "/home/user/project"

class TestSessionDirForCwd:
    """Tests for PiConnectionManager._session_dir_for_cwd."""

    def test_simple_path(self):
        mgr = PiConnectionManager()
        assert mgr._session_dir_for_cwd("/home/user/project") == "--home-user-project--"

    def test_root_path(self):
        mgr = PiConnectionManager()
        assert mgr._session_dir_for_cwd("/") == "----"


class TestExtractSessionId:
    """Tests for PiConnectionManager._extract_session_id."""

    def test_with_uuid_prefix(self):
        mgr = PiConnectionManager()
        assert mgr._extract_session_id("session_abc-123.jsonl") == "abc-123"

    def test_without_prefix(self):
        mgr = PiConnectionManager()
        assert mgr._extract_session_id("abc-123.jsonl") == "abc-123"


class TestFindSessionFile:
    """Tests for PiConnectionManager._find_session_file."""

    @pytest.fixture
    def mgr(self, tmp_path):
        return PiConnectionManager(session_dir=str(tmp_path))

    def _write_session(self, base_dir, relative_path, sid, mtime=None):
        full_path = os.path.join(base_dir, relative_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        header = {
            "type": "session",
            "id": sid,
            "cwd": "/tmp",
            "messageCount": 0,
        }
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
        if mtime is not None:
            os.utime(full_path, (mtime, mtime))
        return full_path

    def test_list_sessions_filters_by_recorded_cwd(self, tmp_path, mgr):
        first = self._write_session(
            str(tmp_path), "flat-one.jsonl", "one", mtime=time.time()
        )
        second = self._write_session(
            str(tmp_path), "nested/flat-two.jsonl", "two", mtime=time.time()
        )
        for path, cwd in ((first, "/tmp/work-a"), (second, "/tmp/work-b")):
            lines = open(path, encoding="utf-8").readlines()
            header = json.loads(lines[0])
            header["cwd"] = cwd
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(header) + "\n")

        sessions, total = mgr.list_sessions("/tmp/work-b")

        assert total == 1
        assert sessions[0].session_id == "two"
        assert sessions[0].cwd == "/tmp/work-b"

    def test_inspect_and_delete_legacy_session(self, tmp_path, mgr):
        path = self._write_session(
            str(tmp_path), "session_legacy-session.jsonl", "legacy-session"
        )
        info = mgr.inspect_session("legacy-session")
        assert info.session_file == path

        import asyncio
        event = types.SimpleNamespace()
        asyncio.run(mgr.delete_session(event, "legacy-session"))
        assert not os.path.exists(path)

        path = self._write_session(str(tmp_path), "abc.jsonl", "abc-123")
        result = mgr._find_session_file(path)
        assert result == path

    def test_exact_match(self, tmp_path, mgr):
        self._write_session(str(tmp_path), "s_abc-123.jsonl", "abc-123")
        result = mgr._find_session_file("abc-123")
        assert result.endswith("s_abc-123.jsonl")

    def test_prefix_match_unique(self, tmp_path, mgr):
        self._write_session(str(tmp_path), "s_abc-123.jsonl", "abc-123")
        result = mgr._find_session_file("abc")
        assert result.endswith("s_abc-123.jsonl")

    def test_prefix_match_multiple_raises(self, tmp_path, mgr):
        self._write_session(str(tmp_path), "s_abc-123.jsonl", "abc-123")
        self._write_session(str(tmp_path), "s_abc-456.jsonl", "abc-456")
        with pytest.raises(PiError, match="Multiple sessions match"):
            mgr._find_session_file("abc")

    def test_no_match(self, tmp_path, mgr):
        self._write_session(str(tmp_path), "s_abc-123.jsonl", "abc-123")
        result = mgr._find_session_file("xyz")
        assert result is None


class TestFindMostRecentSession:
    """Tests for PiConnectionManager._find_most_recent_session."""

    @pytest.fixture
    def mgr(self, tmp_path):
        return PiConnectionManager(session_dir=str(tmp_path))

    def _write_session(self, base_dir, relative_path, sid, mtime=None):
        full_path = os.path.join(base_dir, relative_path)
        header = {
            "type": "session",
            "id": sid,
            "cwd": "/tmp",
            "messageCount": 0,
        }
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
        if mtime is not None:
            os.utime(full_path, (mtime, mtime))
        return full_path

    def test_finds_most_recent(self, tmp_path, mgr):
        now = time.time()
        older = self._write_session(str(tmp_path), "a.jsonl", "a", mtime=now - 10)
        newer = self._write_session(str(tmp_path), "b.jsonl", "b", mtime=now)
        result = mgr._find_most_recent_session()
        assert result == newer
        assert result != older

    def test_no_sessions(self, mgr):
        result = mgr._find_most_recent_session()
        assert result is None


class TestReadSessionHeader:
    """Tests for PiConnectionManager._read_session_header."""

    def test_valid_header(self, tmp_path):
        mgr = PiConnectionManager()
        path = str(tmp_path / "test.jsonl")
        header = {
            "type": "session",
            "id": "sid-123",
            "cwd": "/home/user",
            "name": "test",
            "messageCount": 5,
            "timestamp": "2026-01-01",
        }
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
        info = mgr._read_session_header(path)
        assert info is not None
        assert info.session_id == "sid-123"
        assert info.cwd == "/home/user"
        assert info.session_name == "test"
        assert info.message_count == 5
        assert info.timestamp == "2026-01-01"

    def test_invalid_type(self, tmp_path):
        mgr = PiConnectionManager()
        path = str(tmp_path / "test.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "message"}) + "\n")
        info = mgr._read_session_header(path)
        assert info is None

    def test_empty_file(self, tmp_path):
        mgr = PiConnectionManager()
        path = str(tmp_path / "test.jsonl")
        with open(path, "w", encoding="utf-8"):
            pass
        info = mgr._read_session_header(path)
        assert info is None


class TestFormatSessionInfo:
    """Tests for PiConnectionManager._format_session_info."""

    def test_format(self):
        mgr = PiConnectionManager()
        state = {
            "sessionId": "sid",
            "sessionFile": "/tmp/s.jsonl",
            "sessionName": "my-session",
            "messageCount": 3,
            "thinkingLevel": "high",
            "isStreaming": True,
            "timestamp": "2026-01-01",
        }
        conn = types.SimpleNamespace(cwd="/home/user")
        info = mgr._format_session_info(state, conn)
        assert info.session_id == "sid"
        assert info.session_file == "/tmp/s.jsonl"
        assert info.cwd == "/home/user"
        assert info.session_name == "my-session"
        assert info.message_count == 3
        assert info.thinking_level == "high"
        assert info.is_streaming is True
        assert info.timestamp == "2026-01-01"

    def test_format_defaults(self):
        mgr = PiConnectionManager()
        conn = types.SimpleNamespace(cwd=None)
        info = mgr._format_session_info({}, conn)
        assert info.cwd == ""
        assert info.session_id == ""
        assert info.session_name is None


class TestListSessions:
    """Tests for PiConnectionManager.list_sessions."""

    @pytest.fixture
    def mgr(self, tmp_path):
        return PiConnectionManager(session_dir=str(tmp_path))

    def _write_session(self, base_dir, relative_path, sid, cwd="/tmp", timestamp=None):
        full_path = os.path.join(base_dir, relative_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        header = {
            "type": "session",
            "id": sid,
            "cwd": cwd,
            "messageCount": 0,
        }
        if timestamp is not None:
            header["timestamp"] = timestamp
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
        return full_path

    def test_list_all_sessions(self, tmp_path, mgr):
        self._write_session(str(tmp_path), "a.jsonl", "a")
        self._write_session(str(tmp_path), "nested/b.jsonl", "b")
        sessions, total = mgr.list_sessions()
        assert len(sessions) == 2
        assert total == 2
        ids = {s.session_id for s in sessions}
        assert ids == {"a", "b"}

    def test_list_by_directory(self, tmp_path, mgr):
        cwd = "/home/user/project"
        target_dir = mgr._session_dir_for_cwd(cwd)
        self._write_session(str(tmp_path), f"{target_dir}/a.jsonl", "a", cwd=cwd)
        self._write_session(str(tmp_path), "other/b.jsonl", "b", cwd="/other")
        sessions, total = mgr.list_sessions(directory=cwd)
        assert len(sessions) == 1
        assert total == 1
        assert sessions[0].session_id == "a"

    def test_list_by_directory_trailing_slash(self, tmp_path, mgr):
        cwd = "/home/user/project"
        target_dir = mgr._session_dir_for_cwd(cwd)
        self._write_session(str(tmp_path), f"{target_dir}/a.jsonl", "a", cwd=cwd)
        sessions, total = mgr.list_sessions(directory="/home/user/project/")
        assert len(sessions) == 1
        assert total == 1
        assert sessions[0].session_id == "a"

    def test_list_by_directory_relative(self, tmp_path, mgr, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        rel_dir = "code/project"
        abs_dir = os.path.join(str(tmp_path), rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        target_dir = mgr._session_dir_for_cwd(abs_dir)
        self._write_session(str(tmp_path), f"{target_dir}/a.jsonl", "a", cwd=abs_dir)
        sessions, total = mgr.list_sessions(directory=rel_dir)
        assert len(sessions) == 1
        assert total == 1
        assert sessions[0].session_id == "a"

    def test_list_no_session_root(self, tmp_path):
        mgr = PiConnectionManager(session_dir=str(tmp_path / "nonexistent"))
        assert mgr.list_sessions() == ([], 0)

    def test_list_sessions_sorts_by_timestamp_descending(self, tmp_path, mgr):
        cwd = "/home/user/project"
        target_dir = mgr._session_dir_for_cwd(cwd)
        self._write_session(
            str(tmp_path), f"{target_dir}/a.jsonl", "a", cwd=cwd, timestamp="2026-01-01"
        )
        self._write_session(
            str(tmp_path), f"{target_dir}/b.jsonl", "b", cwd=cwd, timestamp="2026-01-03"
        )
        self._write_session(
            str(tmp_path), f"{target_dir}/c.jsonl", "c", cwd=cwd, timestamp="2026-01-02"
        )
        sessions, _ = mgr.list_sessions(directory=cwd)
        assert [s.session_id for s in sessions] == ["b", "c", "a"]

    def test_list_sessions_pagination(self, tmp_path, mgr):
        cwd = "/home/user/project"
        target_dir = mgr._session_dir_for_cwd(cwd)
        for sid, ts in [
            ("a", "2026-01-01"),
            ("b", "2026-01-02"),
            ("c", "2026-01-03"),
        ]:
            self._write_session(
                str(tmp_path), f"{target_dir}/{sid}.jsonl", sid, cwd=cwd, timestamp=ts
            )
        page, total = mgr.list_sessions(directory=cwd, offset=0, limit=2)
        assert [s.session_id for s in page] == ["c", "b"]
        assert total == 3
        page2, _ = mgr.list_sessions(directory=cwd, offset=2, limit=2)
        assert [s.session_id for s in page2] == ["a"]

    def test_first_message_snippet(self, tmp_path, mgr):
        cwd = "/home/user/project"
        target_dir = mgr._session_dir_for_cwd(cwd)
        full_path = os.path.join(str(tmp_path), target_dir, "a.jsonl")
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        header = {
            "type": "session",
            "id": "a",
            "cwd": cwd,
            "messageCount": 1,
        }
        user_message = {
            "type": "message",
            "message": {"role": "user", "content": "Hello world this is a test"},
        }
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
            f.write(json.dumps(user_message) + "\n")
        sessions, total = mgr.list_sessions(directory=cwd)
        assert total == 1
        assert sessions[0].first_message_snippet == "Hello wor…"

    def test_first_message_snippet_truncation(self, tmp_path, mgr):
        cwd = "/home/user/project"
        target_dir = mgr._session_dir_for_cwd(cwd)
        full_path = os.path.join(str(tmp_path), target_dir, "a.jsonl")
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        header = {
            "type": "session",
            "id": "a",
            "cwd": cwd,
            "messageCount": 1,
        }
        long_text = "a" * 100
        user_message = {
            "type": "message",
            "message": {"role": "user", "content": long_text},
        }
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
            f.write(json.dumps(user_message) + "\n")
        sessions, _ = mgr.list_sessions(directory=cwd)
        snippet = sessions[0].first_message_snippet
        assert snippet is not None
        assert len(snippet) <= 10
        assert snippet.endswith("…")

    @pytest.mark.asyncio
    async def test_get_active_cwd_fallback_to_default(self):
        mgr = PiConnectionManager()
        event = types.SimpleNamespace(
            get_platform_name=lambda: "qq",
            get_group_id=lambda: "123",
            get_sender_id=lambda: "456",
        )
        mgr.set_active_cwd(event, "/home/user/project/")
        result = await mgr.get_active_cwd(event)
        assert result == "/home/user/project"
