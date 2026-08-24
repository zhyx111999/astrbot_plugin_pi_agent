"""Shared pytest fixtures and mocks for the Pi Agent test suite.

This module sets up fake AstrBot public API modules so that `main.py` can be
imported in tests without the full AstrBot runtime.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Shared test setup before importing plugin modules.
# isort: off
import _helpers  # noqa: F401
# isort: on

# Ensure the project root is on sys.path so plugin modules can be imported.
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


class FakeAstrMessageEvent:
    """Minimal stand-in for AstrMessageEvent."""

    def __init__(self, message_str: str = "", admin: bool = False):
        self.message_str = message_str
        self._admin = admin

    def is_admin(self) -> bool:
        return self._admin

    def plain_result(self, text: str) -> str:
        return text


class FakeContext:
    """Minimal stand-in for astrbot.api.star.Context."""


class FakeStar:
    """Minimal stand-in for astrbot.api.star.Star."""

    def __init__(self, context: FakeContext, config=None):
        pass


def _fake_command(name: str):
    """Decorator that records the command name on the handler."""

    def decorator(func):
        func.__command_name__ = name
        return func

    return decorator


def _fake_llm_tool(name: str | None = None, **kwargs):
    """Decorator that records the LLM tool name on the handler."""

    def decorator(func):
        func.__llm_tool_name__ = name or func.__name__
        return func

    return decorator


fake_filter = types.ModuleType("filter")
fake_filter.command = _fake_command
fake_filter.llm_tool = _fake_llm_tool
fake_event = types.ModuleType("astrbot.api.event")
fake_event.AstrMessageEvent = FakeAstrMessageEvent
fake_event.filter = fake_filter

fake_star = types.ModuleType("astrbot.api.star")
fake_star.Context = FakeContext
fake_star.Star = FakeStar

fake_astrbot_path = types.ModuleType("astrbot.core.utils.astrbot_path")
fake_astrbot_path.get_astrbot_data_path = lambda: str(
    Path(__file__).resolve().parent / "data"
)

fake_core = types.ModuleType("astrbot.core")
fake_core_utils = types.ModuleType("astrbot.core.utils")
fake_core_utils.astrbot_path = fake_astrbot_path
fake_core.utils = fake_core_utils

# Register the fake modules so `import main` can resolve without AstrBot installed.
sys.modules["astrbot.api.event"] = fake_event
sys.modules["astrbot.api.star"] = fake_star
sys.modules["astrbot.core"] = fake_core
sys.modules["astrbot.core.utils"] = fake_core_utils
sys.modules["astrbot.core.utils.astrbot_path"] = fake_astrbot_path

import main  # noqa: E402


@pytest.fixture
def plugin():
    """Return a PiAgentPlugin instance with a mocked connection manager."""
    instance = main.PiAgentPlugin(context=FakeContext())
    instance.pi_connection_manager = MagicMock()
    yield instance


@pytest.fixture
def admin_event():
    """Return a FakeAstrMessageEvent with admin privileges."""
    return FakeAstrMessageEvent(admin=True)


@pytest.fixture
def non_admin_event():
    """Return a FakeAstrMessageEvent without admin privileges."""
    return FakeAstrMessageEvent(admin=False)


@pytest.fixture
def async_iter():
    """Return a helper that yields items from an async iterator."""

    async def _async_iter(items):
        for item in items:
            yield item

    return _async_iter
