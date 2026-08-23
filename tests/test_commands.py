"""Tests for pi_legacy/commands.py."""

# Shared test setup: must run before any pi_legacy import.
# isort: off
import _helpers  # noqa: F401
from pi_legacy.commands import (  # noqa: E402
    _extract_user_text,
    _filter_user_entries,
    extract_active_branch,
    format_commands_list,
    format_session_info,
    format_session_list,
    format_tree_entries,
    format_ui_request,
    parse_subcommand,
    parse_ui_reply_args,
    resolve_select_option,
    resolve_tree_entry_id,
    strip_command_prefix,
)
from pi_legacy.models import SessionInfo, UIRequest  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402
# isort: on

import pytest


class TestStripCommandPrefix:
    def test_removes_slash_prefix(self):
        assert strip_command_prefix("/pi open", "pi") == "open"

    def test_removes_plain_prefix(self):
        assert strip_command_prefix("pi open", "pi") == "open"

    def test_no_match_returns_unchanged(self):
        assert strip_command_prefix("hello", "pi") == "hello"

    def test_strips_surrounding_whitespace(self):
        assert strip_command_prefix("  /pi open  ", "pi") == "open"


class TestParseSubcommand:
    def test_empty_returns_empty(self):
        assert parse_subcommand("") == ("", "")

    def test_single_word(self):
        assert parse_subcommand("open") == ("open", "")

    def test_lowercase_subcommand(self):
        assert parse_subcommand("OPEN path") == ("open", "path")

    def test_with_rest(self):
        assert parse_subcommand("resume abc123 extra") == ("resume", "abc123 extra")


class TestFormatSessionInfo:
    def test_basic_format(self):
        info = SessionInfo(
            session_id="sid-1",
            session_file="/tmp/session.jsonl",
            cwd="/home/user/project",
            session_name="my session",
            message_count=42,
        )
        output = format_session_info(info)
        expected = (
            "Session: sid-1\n"
            "Name: my session\n"
            "Working directory: /home/user/project\n"
            "File: /tmp/session.jsonl\n"
            "Messages: 42"
        )
        assert output == expected

    def test_unnamed_placeholder(self):
        info = SessionInfo(
            session_id="sid-2",
            session_file="/tmp/session.jsonl",
            cwd="/home/user/project",
            message_count=0,
        )
        assert "Name: (unnamed)" in format_session_info(info)

    def test_thinking_level_appended(self):
        info = SessionInfo(
            session_id="sid-3",
            session_file="/tmp/session.jsonl",
            cwd="/home/user/project",
            thinking_level="high",
        )
        assert "Thinking level: high" in format_session_info(info)


class TestFormatSessionList:
    def test_empty_list(self):
        assert format_session_list([]) == "No sessions found."

    def test_multiple_sessions(self):
        sessions = [
            SessionInfo(
                session_id="sid-1",
                session_file="/tmp/a.jsonl",
                cwd="/home/a",
                session_name="alpha",
                message_count=1,
                timestamp="2026-07-01",
                first_message_snippet="hello world",
            ),
            SessionInfo(
                session_id="sid-2",
                session_file="/tmp/b.jsonl",
                cwd="/home/b",
                message_count=2,
            ),
        ]
        output = format_session_list(sessions, total=2)
        assert "Available sessions:" in output
        assert "1. alpha" in output
        assert "2. (unnamed)" in output
        assert "ID: sid-2" in output
        assert "Created: unknown" in output
        assert "First message: hello world" in output
        assert "First message: (no preview)" in output
        assert "CWD: /home/a" in output
        assert "CWD: /home/b" in output

    def test_hides_cwd_when_directory_given(self):
        sessions = [
            SessionInfo(
                session_id="sid-1",
                session_file="/tmp/a.jsonl",
                cwd="/home/a",
                session_name="alpha",
                message_count=1,
                timestamp="2026-07-01",
            )
        ]
        output = format_session_list(sessions, directory="/home/a", total=1)
        assert "CWD:" not in output
        assert "First message:" in output

    def test_single_page_no_footer(self):
        sessions = [
            SessionInfo(
                session_id="sid-1",
                session_file="/tmp/a.jsonl",
                cwd="/home/a",
                session_name="alpha",
                message_count=1,
                timestamp="2026-07-01",
            )
        ]
        output = format_session_list(sessions, total=1)
        assert "Page" not in output
        assert "Use /pi next" not in output
        assert "End of list" not in output

    def test_pagination_footer(self):
        sessions = [
            SessionInfo(
                session_id="sid-1",
                session_file="/tmp/a.jsonl",
                cwd="/home/a",
                session_name="alpha",
                message_count=1,
                timestamp="2026-07-01",
            )
        ]
        output = format_session_list(sessions, page=1, page_size=10, total=25)
        assert "Page 1/3" in output
        assert "Use /pi next to view more" in output

    def test_last_page_footer(self):
        sessions = [
            SessionInfo(
                session_id="sid-1",
                session_file="/tmp/a.jsonl",
                cwd="/home/a",
                session_name="alpha",
                message_count=1,
                timestamp="2026-07-01",
            )
        ]
        output = format_session_list(sessions, page=3, page_size=10, total=25)
        assert "Page 3/3" in output
        assert "End of list" in output

    def test_start_index_respects_page(self):
        sessions = [
            SessionInfo(
                session_id="sid-1",
                session_file="/tmp/a.jsonl",
                cwd="/home/a",
                session_name="alpha",
                message_count=1,
                timestamp="2026-07-01",
            )
        ]
        output = format_session_list(sessions, page=2, page_size=10, total=11)
        assert "11. alpha" in output


class TestFormatUiRequest:
    def test_confirm(self):
        req = UIRequest(local_id=1, request_id="r1", method="confirm", title="Ok?")
        output = format_ui_request(req)
        assert "[Request #1]" in output
        assert "Title: Ok?" in output
        assert "/pi confirm 1 yes" in output
        assert "/pi confirm 1 no" in output

    def test_select(self):
        req = UIRequest(
            local_id=2,
            request_id="r2",
            method="select",
            message="Choose",
            options=["a", "b"],
        )
        output = format_ui_request(req)
        assert "Options:" in output
        assert "1. a" in output
        assert "2. b" in output
        assert "/pi select 2 <option>" in output

    def test_input(self):
        req = UIRequest(local_id=3, request_id="r3", method="input")
        assert "/pi input 3 <value>" in format_ui_request(req)

    def test_editor_with_prefill(self):
        req = UIRequest(
            local_id=4,
            request_id="r4",
            method="editor",
            prefill="hello",
        )
        output = format_ui_request(req)
        assert "Prefill: hello" in output
        assert "/pi edit 4 <text>" in output

    def test_unknown_method_defaults_to_input(self):
        req = UIRequest(local_id=5, request_id="r5", method="unknown")
        assert "/pi input 5 <value>" in format_ui_request(req)


class TestFormatCommandsList:
    def test_empty(self):
        assert format_commands_list([]) == "No slash commands available."

    def test_with_descriptions(self):
        commands = [
            {"name": "explore", "description": "Explore", "source": "pi"},
            {"name": "ask", "description": "Ask", "source": "pi"},
        ]
        output = format_commands_list(commands)
        assert output.startswith("Available pi commands:\n\n")
        assert "/explore (pi) ➡️ Explore" in output
        assert "/ask (pi) ➡️ Ask" in output
        # Two command lines separated by a blank line -> a single blank line between them.
        assert "Explore\n\n/ask" in output

    def test_without_description(self):
        commands = [{"name": "custom", "description": "", "source": "builtin"}]
        assert "/custom (builtin)" in format_commands_list(commands)


class TestResolveSelectOption:
    @pytest.fixture
    def select_request(self):
        return UIRequest(
            local_id=1,
            request_id="r1",
            method="select",
            options=["apple", "banana"],
        )

    def test_no_options_returns_value(self):
        req = UIRequest(local_id=1, request_id="r1", method="select")
        assert resolve_select_option(req, "anything") == "anything"

    def test_exact_match(self, select_request):
        assert resolve_select_option(select_request, "banana") == "banana"

    def test_index_match(self, select_request):
        assert resolve_select_option(select_request, "2") == "banana"

    def test_invalid_returns_none(self, select_request):
        assert resolve_select_option(select_request, "3") is None
        assert resolve_select_option(select_request, "grape") is None


class TestParseUiReplyArgs:
    def test_empty_returns_none(self):
        assert parse_ui_reply_args("") == (None, "")

    def test_only_id(self):
        assert parse_ui_reply_args("7") == (7, "")

    def test_id_and_value(self):
        assert parse_ui_reply_args("7 yes") == (7, "yes")

    def test_id_and_value_with_spaces(self):
        assert parse_ui_reply_args("7 yes please") == (7, "yes please")

    def test_invalid_id_returns_raw(self):
        assert parse_ui_reply_args("abc yes") == (None, "abc yes")


class TestExtractActiveBranch:
    def test_empty_tree(self):
        assert extract_active_branch([], "leaf") == []

    def test_no_leaf_id(self):
        tree = [{"entry": {"id": "root"}, "children": []}]
        assert extract_active_branch(tree, None) == []

    def test_single_root(self):
        tree = [{"entry": {"id": "root"}, "children": []}]
        assert extract_active_branch(tree, "root") == [{"id": "root"}]

    def test_branch_from_root_to_leaf(self):
        tree = [
            {
                "entry": {"id": "root"},
                "children": [
                    {
                        "entry": {"id": "child", "parentId": "root"},
                        "children": [
                            {
                                "entry": {"id": "leaf", "parentId": "child"},
                                "children": [],
                            }
                        ],
                    }
                ],
            }
        ]
        branch = extract_active_branch(tree, "leaf")
        assert [e["id"] for e in branch] == ["root", "child", "leaf"]

    def test_missing_node_breaks_loop(self):
        tree = [{"entry": {"id": "root"}, "children": []}]
        assert extract_active_branch(tree, "missing") == []

    def test_cycle_avoided(self):
        tree = [
            {
                "entry": {"id": "a", "parentId": "c"},
                "children": [
                    {
                        "entry": {"id": "b", "parentId": "a"},
                        "children": [
                            {
                                "entry": {"id": "c", "parentId": "b"},
                                "children": [],
                            }
                        ],
                    }
                ],
            }
        ]
        branch = extract_active_branch(tree, "c")
        assert [e["id"] for e in branch] == ["a", "b", "c"]


class TestFilterUserEntries:
    def test_filters_non_message(self):
        entries = [
            {"type": "action"},
            {"type": "message", "message": {"role": "user"}},
        ]
        assert _filter_user_entries(entries) == [entries[1]]

    def test_excludes_assistant(self):
        entries = [{"type": "message", "message": {"role": "assistant"}}]
        assert _filter_user_entries(entries) == []


class TestExtractUserText:
    def test_string_content(self):
        entry = {"message": {"content": "Hello\nworld"}}
        assert _extract_user_text(entry) == "Hello world"

    def test_text_blocks(self):
        entry = {
            "message": {
                "content": [
                    {"type": "text", "text": "Hello "},
                    {"type": "text", "text": "world"},
                ]
            }
        }
        assert _extract_user_text(entry) == "Hello world"

    def test_empty_returns_placeholder(self):
        assert _extract_user_text({"message": {}}) == "(empty)"

    def test_truncation(self):
        entry = {"message": {"content": "a" * 100}}
        assert _extract_user_text(entry, max_length=20).endswith("…")


class TestFormatTreeEntries:
    def test_no_user_messages(self):
        assert (
            format_tree_entries([])
            == "No user messages on the active branch."
        )

    def test_user_messages_formatted(self):
        entries = [
            {
                "type": "message",
                "message": {"role": "user", "content": "hello"},
                "id": "e1",
                "timestamp": "2026-07-01T12:00:00",
            }
        ]
        output = format_tree_entries(entries)
        assert "User messages on the active branch:" in output
        assert "1. 2026-07-01 hello" in output
        assert "/pi tree <number>" in output


class TestResolveTreeEntryId:
    def test_valid_number(self):
        entries = [
            {"type": "message", "message": {"role": "user"}, "id": "e1"},
            {"type": "message", "message": {"role": "user"}, "id": "e2"},
        ]
        assert resolve_tree_entry_id(entries, 2) == "e2"

    def test_invalid_number(self):
        entries = [
            {"type": "message", "message": {"role": "user"}, "id": "e1"},
        ]
        assert resolve_tree_entry_id(entries, 0) is None
        assert resolve_tree_entry_id(entries, 5) is None


class TestSessionHandlers:
    """Tests for /pi sessions and /pi next handler logic."""

    @pytest.mark.asyncio
    async def test_sessions_handler_lists_first_page(self, plugin, admin_event):
        plugin.pi_connection_manager = MagicMock()
        plugin.pi_connection_manager.list_sessions.return_value = (
            [
                SessionInfo(
                    session_id="sid-1",
                    session_file="/tmp/a.jsonl",
                    cwd="/home/a",
                    session_name="alpha",
                    message_count=1,
                    timestamp="2026-07-01",
                )
            ],
            1,
        )
        results = [
            r async for r in plugin._handle_pi_sessions(admin_event, "/home/a")
        ]
        assert len(results) == 1
        assert "Available sessions" in results[0]
        plugin.pi_connection_manager.set_active_cwd.assert_called_once_with(
            admin_event, "/home/a"
        )
        plugin.pi_connection_manager.set_last_sessions_query.assert_called_once()

    @pytest.mark.asyncio
    async def test_sessions_handler_uses_active_cwd(self, plugin, admin_event):
        plugin.pi_connection_manager = MagicMock()
        plugin.pi_connection_manager.get_active_cwd = AsyncMock(
            return_value="/home/a"
        )
        plugin.pi_connection_manager.list_sessions.return_value = ([], 0)
        results = [r async for r in plugin._handle_pi_sessions(admin_event, "")]
        assert len(results) == 1
        assert "No sessions found" in results[0]
        plugin.pi_connection_manager.list_sessions.assert_called_once_with(
            "/home/a", offset=0, limit=10
        )

    @pytest.mark.asyncio
    async def test_next_handler_returns_next_page(self, plugin, admin_event):
        plugin.pi_connection_manager = MagicMock()
        plugin.pi_connection_manager.get_last_sessions_query.return_value = {
            "directory": "/home/a",
            "page": 1,
            "page_size": 10,
            "total": 25,
        }
        plugin.pi_connection_manager.list_sessions.return_value = (
            [
                SessionInfo(
                    session_id="sid-11",
                    session_file="/tmp/a.jsonl",
                    cwd="/home/a",
                    session_name="kappa",
                    message_count=1,
                    timestamp="2026-07-01",
                )
            ],
            25,
        )
        results = [r async for r in plugin._handle_pi_next(admin_event)]
        assert len(results) == 1
        assert "Available sessions" in results[0]
        assert "Page 2/3" in results[0]
        plugin.pi_connection_manager.list_sessions.assert_called_once_with(
            "/home/a", offset=10, limit=10
        )

    @pytest.mark.asyncio
    async def test_next_handler_without_query(self, plugin, admin_event):
        plugin.pi_connection_manager = MagicMock()
        plugin.pi_connection_manager.get_last_sessions_query.return_value = None
        results = [r async for r in plugin._handle_pi_next(admin_event)]
        assert len(results) == 1
        assert "No previous sessions list" in results[0]
