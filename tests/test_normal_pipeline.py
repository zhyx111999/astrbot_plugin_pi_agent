"""Tests for the public AstrBot normal-pipeline relay."""

from __future__ import annotations

# isort: off
import _helpers  # noqa: F401
# isort: on

import sys
import types

import pytest

from pi_agent_bridge.normal_pipeline import (
    NormalPipelineRelayError,
    enqueue_progress_wakeup,
    enqueue_terminal_wakeup,
)


class _Plain:
    def __init__(self, text: str):
        self.text = text


class _Member:
    def __init__(self, user_id: str, nickname: str | None = None):
        self.user_id = user_id
        self.nickname = nickname


@pytest.mark.asyncio
async def test_terminal_wakeup_enters_normal_event_queue(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class StarTools:
        @staticmethod
        async def create_message(**kwargs):
            calls.append(("message", kwargs))
            return "synthetic-event"

        @staticmethod
        async def create_event(event, **kwargs):
            calls.append(("event", {"event": event, **kwargs}))

    message_components = types.ModuleType("astrbot.api.message_components")
    message_components.Plain = _Plain
    platform = types.ModuleType("astrbot.api.platform")
    platform.MessageMember = _Member
    star = types.ModuleType("astrbot.api.star")
    star.StarTools = StarTools
    for name, module in {
        "astrbot.api.message_components": message_components,
        "astrbot.api.platform": platform,
        "astrbot.api.star": star,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    await enqueue_terminal_wakeup(
        context=object(),
        session_origin="snowluma:GroupMessage:group-123",
        message="terminal wake metadata",
    )

    message_call = calls[0][1]
    event_call = calls[1][1]
    assert message_call["message_str"] == "terminal wake metadata"
    assert message_call["sender"].user_id == "group-123"
    assert message_call["group_id"] == "group-123"
    assert message_call["raw_message"] == {
        "origin": "astrbot_plugin_pi_agent",
        "kind": "terminal_wakeup",
    }
    assert event_call == {
        "event": "synthetic-event",
        "platform": "snowluma",
        "is_wake": True,
    }


@pytest.mark.asyncio
async def test_progress_wakeup_marks_progress_kind(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class StarTools:
        @staticmethod
        async def create_message(**kwargs):
            calls.append(("message", kwargs))
            return "synthetic-event"

        @staticmethod
        async def create_event(event, **kwargs):
            calls.append(("event", {"event": event, **kwargs}))

    message_components = types.ModuleType("astrbot.api.message_components")
    message_components.Plain = _Plain
    platform = types.ModuleType("astrbot.api.platform")
    platform.MessageMember = _Member
    star = types.ModuleType("astrbot.api.star")
    star.StarTools = StarTools
    for name, module in {
        "astrbot.api.message_components": message_components,
        "astrbot.api.platform": platform,
        "astrbot.api.star": star,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    await enqueue_progress_wakeup(
        context=object(),
        session_origin="snowluma:FriendMessage:3268514224",
        message="intermediate progress",
    )

    assert calls[0][1]["raw_message"]["kind"] == "progress_wakeup"


@pytest.mark.asyncio
async def test_group_terminal_wakeup_uses_configured_wake_prefix(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class Context:
        def get_config(self, session_origin):
            assert session_origin == "snowluma:GroupMessage:group-123"
            return {"wake_prefix": ["+"]}

    class StarTools:
        @staticmethod
        async def create_message(**kwargs):
            calls.append(("message", kwargs))
            return "synthetic-event"

        @staticmethod
        async def create_event(event, **kwargs):
            calls.append(("event", {"event": event, **kwargs}))

    message_components = types.ModuleType("astrbot.api.message_components")
    message_components.Plain = _Plain
    platform = types.ModuleType("astrbot.api.platform")
    platform.MessageMember = _Member
    star = types.ModuleType("astrbot.api.star")
    star.StarTools = StarTools
    for name, module in {
        "astrbot.api.message_components": message_components,
        "astrbot.api.platform": platform,
        "astrbot.api.star": star,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    await enqueue_terminal_wakeup(
        context=Context(),
        session_origin="snowluma:GroupMessage:group-123",
        message="terminal wake metadata",
    )

    assert calls[0][1]["message_str"] == "+ terminal wake metadata"
    assert calls[0][1]["message"][0].text == "+ terminal wake metadata"


@pytest.mark.asyncio
async def test_terminal_wakeup_uses_original_private_sender(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class StarTools:
        @staticmethod
        async def create_message(**kwargs):
            calls.append(("message", kwargs))
            return "synthetic-event"

        @staticmethod
        async def create_event(event, **kwargs):
            calls.append(("event", {"event": event, **kwargs}))

    message_components = types.ModuleType("astrbot.api.message_components")
    message_components.Plain = _Plain
    platform = types.ModuleType("astrbot.api.platform")
    platform.MessageMember = _Member
    star = types.ModuleType("astrbot.api.star")
    star.StarTools = StarTools
    for name, module in {
        "astrbot.api.message_components": message_components,
        "astrbot.api.platform": platform,
        "astrbot.api.star": star,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    await enqueue_terminal_wakeup(
        context=object(),
        session_origin="snowluma:FriendMessage:3268514224",
        message="terminal wake metadata",
    )

    assert calls[0][1]["sender"].user_id == "3268514224"
    assert calls[0][1]["group_id"] == ""


@pytest.mark.asyncio
async def test_terminal_wakeup_rejects_invalid_session(monkeypatch):
    message_components = types.ModuleType("astrbot.api.message_components")
    message_components.Plain = _Plain
    platform = types.ModuleType("astrbot.api.platform")
    platform.MessageMember = _Member
    star = types.ModuleType("astrbot.api.star")
    star.StarTools = types.SimpleNamespace()
    for name, module in {
        "astrbot.api.message_components": message_components,
        "astrbot.api.platform": platform,
        "astrbot.api.star": star,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    with pytest.raises(NormalPipelineRelayError, match="invalid task session"):
        await enqueue_terminal_wakeup(
            context=object(),
            session_origin="bad",
            message="wake",
        )
