"""Focused tests for the independent Pi RPC bridge."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from pi_agent_bridge.rpc import PiProcessState, PiRpcAdapter, PiRpcError


class FakeReader:
    def __init__(self) -> None:
        self._items: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self._items.get()

    async def feed(self, value: dict[str, Any] | str) -> None:
        if isinstance(value, dict):
            value = json.dumps(value)
        await self._items.put(value.encode("utf-8") + b"\n")

    async def close(self) -> None:
        await self._items.put(b"")


class FakeWriter:
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.lines.append(json.loads(data.decode("utf-8")))

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed

    def write_eof(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeWriter()
        self.stdout = FakeReader()
        self.stderr = FakeReader()
        self.returncode: int | None = None
        self._waiter = asyncio.Event()

    def reset(self) -> None:
        """Make the fake reusable when a recovery launch uses one instance."""

        self.stdin = FakeWriter()
        self.stdout = FakeReader()
        self.stderr = FakeReader()
        self.returncode = None
        self._waiter = asyncio.Event()

    async def wait(self) -> int:
        await self._waiter.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.returncode = 0
        self._waiter.set()

    def kill(self) -> None:
        self.returncode = -9
        self._waiter.set()


@pytest.fixture
def fake_process() -> FakeProcess:
    return FakeProcess()


@pytest.fixture
def factory(fake_process: FakeProcess):
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def _factory(*args: str, **kwargs: Any) -> FakeProcess:
        if fake_process.returncode is not None:
            fake_process.reset()
        calls.append((args, kwargs))
        return fake_process

    _factory.calls = calls  # type: ignore[attr-defined]
    return _factory


@pytest.mark.asyncio
async def test_prompt_is_written_without_waiting_for_agent_end(
    fake_process: FakeProcess, factory
) -> None:
    adapter = PiRpcAdapter(
        task_id="task-1",
        session_dir="/tmp/pi-sessions",
        cwd="/tmp/project",
        process_factory=factory,
    )
    await adapter.start()

    command_id = await adapter.send_prompt_nowait("long task")
    assert command_id.startswith("pi-cmd-")
    assert fake_process.stdin.lines == [
        {"type": "prompt", "message": "long task", "id": command_id}
    ]

    await fake_process.stdout.feed({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "working"}})
    await asyncio.sleep(0)
    events = adapter.drain_events()
    assert len(events) == 1
    assert events[0].meaningful is True
    assert adapter.snapshot()["state"] == PiProcessState.RUNNING.value
    await adapter.terminate()


@pytest.mark.asyncio
async def test_control_command_waits_for_ack_but_not_agent_completion(
    fake_process: FakeProcess, factory
) -> None:
    adapter = PiRpcAdapter(process_factory=factory, command_timeout=0.5)
    await adapter.start()

    pending = asyncio.create_task(adapter.get_state())
    await asyncio.sleep(0)
    command = fake_process.stdin.lines[-1]
    await fake_process.stdout.feed({"type": "response", "id": command["id"], "success": True, "data": {"session": "s1"}})
    assert await pending == {"session": "s1"}
    assert adapter.drain_events()[-1].meaningful is False
    await adapter.terminate()


@pytest.mark.asyncio
async def test_meaningful_cursor_ignores_heartbeat_and_duplicate(
    fake_process: FakeProcess, factory
) -> None:
    adapter = PiRpcAdapter(process_factory=factory, event_buffer_size=8)
    await adapter.start()
    event = {"type": "status", "status": "working"}
    await fake_process.stdout.feed({"type": "heartbeat"})
    await fake_process.stdout.feed(event)
    await fake_process.stdout.feed(event)
    await asyncio.sleep(0)
    observed = adapter.drain_events()
    assert [item.meaningful for item in observed] == [False, True, False]
    assert adapter.meaningful_event_cursor == observed[1].cursor
    assert len(adapter.drain_events(meaningful_only=True)) == 1
    await adapter.terminate()


@pytest.mark.asyncio
async def test_identical_consecutive_events_are_not_meaningful_duplicates(
    fake_process: FakeProcess, factory
) -> None:
    adapter = PiRpcAdapter(process_factory=factory)
    await adapter.start()
    delta = {
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_delta", "delta": "same chunk"},
    }
    await fake_process.stdout.feed(delta)
    await fake_process.stdout.feed(delta)
    await asyncio.sleep(0)
    assert [event.meaningful for event in adapter.drain_events()] == [True, False]
    await adapter.terminate()


@pytest.mark.asyncio
async def test_resume_relaunches_with_session_and_recovery_snapshot(
    tmp_path: Path, fake_process: FakeProcess, factory
) -> None:
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","id":"sid"}\n', encoding="utf-8")
    adapter = PiRpcAdapter(session_path=session, process_factory=factory)
    await adapter.start()
    await adapter.terminate()
    await adapter.resume()

    assert adapter.is_running
    assert adapter.recovery_snapshot()["recoverable"] is True
    assert adapter.recovery_snapshot()["session_exists"] is True
    assert len(factory.calls) == 2
    assert factory.calls[1][0][:3] == ("pi", "--mode", "rpc")
    await adapter.terminate()


@pytest.mark.asyncio
async def test_transport_errors_are_structured_exceptions(factory) -> None:
    adapter = PiRpcAdapter(process_factory=factory, command_timeout=0.01)
    await adapter.start()
    with pytest.raises(PiRpcError, match="Timed out"):
        await adapter.get_state()
    await adapter.terminate()


@pytest.mark.asyncio
async def test_worker_environment_does_not_inherit_unselected_host_secrets(
    fake_process: FakeProcess, monkeypatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []

    async def capture_factory(*args: str, **kwargs: Any) -> FakeProcess:
        calls.append(kwargs)
        return fake_process

    monkeypatch.setenv("ASTRBOT_UNRELATED_API_KEY", "host-secret")
    adapter = PiRpcAdapter(
        agent_dir=tmp_path / "agent",
        environment={"PI_SELECTED_KEY": "selected-secret"},
        process_factory=capture_factory,
    )
    await adapter.start()

    child_env = calls[0]["env"]
    assert child_env["PI_SELECTED_KEY"] == "selected-secret"
    assert child_env["PI_CODING_AGENT_DIR"] == str((tmp_path / "agent"))
    assert "ASTRBOT_UNRELATED_API_KEY" not in child_env
    assert adapter.environment == {}
    await adapter.terminate()


@pytest.mark.asyncio
async def test_worker_output_and_control_errors_redact_selected_api_key(
    fake_process: FakeProcess, factory
) -> None:
    secret = "selected-api-key"
    adapter = PiRpcAdapter(
        environment={"PI_SELECTED_KEY": secret},
        process_factory=factory,
    )
    await adapter.start()

    await fake_process.stdout.feed(
        {
            "type": "error",
            "message": f"upstream rejected api_key={secret}",
        }
    )
    await asyncio.sleep(0)
    assert secret not in json.dumps(
        [event.as_dict() for event in adapter.drain_events()]
    )

    pending = asyncio.create_task(adapter.get_state())
    await asyncio.sleep(0)
    command = fake_process.stdin.lines[-1]
    await fake_process.stdout.feed(
        {
            "type": "response",
            "id": command["id"],
            "success": False,
            "error": f"authorization failed: Bearer {secret}",
        }
    )
    with pytest.raises(PiRpcError) as error:
        await pending
    assert secret not in str(error.value)
    assert "[REDACTED]" in str(error.value)
    await adapter.terminate()


def test_sanitizer_redacts_nested_token_and_cookie_fields():
    from pi_agent_bridge.security import sanitize_value

    payload = sanitize_value(
        {
            "token": "token-secret",
            "nested": {"session_cookie": "cookie-secret", "message": "keep"},
        }
    )

    assert payload == {
        "token": "[REDACTED]",
        "nested": {"session_cookie": "[REDACTED]", "message": "keep"},
    }
