"""Asynchronous, task-scoped bridge for Pi's JSONL RPC mode.

This module intentionally contains no AstrBot imports and no task registry
logic.  One :class:`PiRpcAdapter` owns one ``pi --mode rpc`` process.  Prompt
submission is fire-and-forget from the caller's point of view; all output is
collected by a background reader and can be observed through cursors.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, Self

from .runtime import ExecutableCommand
from .security import is_secret_environment_key, safe_error_summary, sanitize_value

logger = logging.getLogger(__name__)


class PiRpcError(RuntimeError):
    """A transport, protocol, or Pi process failure."""


class PiProcessState(StrEnum):
    """Lifecycle states exposed in snapshots."""

    CREATED = "created"
    RUNNING = "running"
    EXITED = "exited"
    TERMINATED = "terminated"
    ORPHANED = "orphaned"


@dataclass(frozen=True, slots=True)
class PiEvent:
    """An observed JSONL event with a monotonically increasing local cursor."""

    cursor: int
    received_at: float
    payload: dict[str, Any]
    meaningful: bool

    @property
    def type(self) -> str | None:
        value = self.payload.get("type")
        return value if isinstance(value, str) else None

    def as_dict(self) -> dict[str, Any]:
        """Return a serializable event envelope for task/result adapters."""

        return {
            "cursor": self.cursor,
            "received_at": self.received_at,
            "type": self.type,
            "meaningful": self.meaningful,
            "payload": self.payload,
        }


class _StreamReader(Protocol):
    async def readline(self) -> bytes: ...


class _StreamWriter(Protocol):
    def write(self, data: bytes) -> Any: ...

    async def drain(self) -> Any: ...

    def is_closing(self) -> bool: ...

    def write_eof(self) -> Any: ...


class _Process(Protocol):
    stdin: _StreamWriter | None
    stdout: _StreamReader | None
    stderr: _StreamReader | None
    returncode: int | None

    async def wait(self) -> int: ...

    def terminate(self) -> Any: ...

    def kill(self) -> Any: ...


ProcessFactory = Callable[..., Awaitable[_Process] | _Process]


def _normalize_executable(executable: ExecutableCommand) -> tuple[str, ...]:
    """Keep a configured executable as a shell-free argv prefix."""

    if isinstance(executable, (str, os.PathLike)):
        values = (os.fspath(executable),)
    elif isinstance(executable, Sequence):
        values = tuple(os.fspath(item) for item in executable)
    else:
        raise TypeError("executable must be a string, path, or sequence")
    if not values or any(not value.strip() for value in values):
        raise ValueError("executable cannot be empty")
    return values


def _command_display(command: Sequence[str]) -> str:
    return " ".join(command)


_MEANINGFUL_TYPES = frozenset(
    {
        "agent_start", "agent_end", "turn_start", "turn_end", "tool_call",
        "tool_start", "tool_end", "tool_result", "tool_execution_start",
        "tool_execution_end", "tool_execution_result", "message_start",
        "message_end", "assistant_message_start", "assistant_message_end",
        "extension_ui_request", "artifact", "artifact_created", "session_created",
        "session_switched", "session_compacted", "compaction_start", "compaction_end",
        "error", "rpc_error", "process_exit",
    }
)
_CHILD_ENVIRONMENT_KEYS = frozenset(
    {
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "LOGNAME",
        "NODE_OPTIONS",
        "NO_PROXY",
        "OS",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WINDIR",
        "WSL_DISTRO_NAME",
        "WSL_INTEROP",
    }
)

_RPC_STREAM_LIMIT = 16 * 1024 * 1024


def _child_environment() -> dict[str, str]:
    """Select process essentials without exposing host provider credentials."""

    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _CHILD_ENVIRONMENT_KEYS or key.upper().endswith("_PROXY")
    }


class PiRpcAdapter:
    """Own one independent Pi RPC process for one long-running task.

    The adapter never waits for an agent turn to finish.  ``send_prompt_nowait``
    only writes the prompt command and returns its command id.  The reader task
    continuously records events, making the adapter safe for periodic polling.
    Control commands (session inspection, steer, abort) may wait briefly for an
    acknowledgement, but have a bounded transport timeout and do not impose a
    task timeout.
    """

    def __init__(
        self,
        *,
        task_id: str | None = None,
        executable: ExecutableCommand = "pi",
        session_path: str | os.PathLike[str] | None = None,
        session_dir: str | os.PathLike[str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        name: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        environment: dict[str, str] | None = None,
        agent_dir: str | os.PathLike[str] | None = None,
        skill_paths: Sequence[str | os.PathLike[str]] | None = None,
        extension_paths: Sequence[str | os.PathLike[str]] | None = None,
        process_factory: ProcessFactory | None = None,
        command_timeout: float = 10.0,
        event_buffer_size: int = 2048,
    ) -> None:
        if event_buffer_size < 1:
            raise ValueError("event_buffer_size must be positive")
        if command_timeout <= 0:
            raise ValueError("command_timeout must be positive")

        self.task_id = task_id or uuid.uuid4().hex
        self.executable = _normalize_executable(executable)
        self.session_path = os.fspath(session_path) if session_path else None
        self.session_dir = os.fspath(session_dir) if session_dir else None
        self.cwd = os.fspath(cwd) if cwd else None
        self.name = name
        self.provider = provider
        self.model = model
        self.agent_dir = os.fspath(agent_dir) if agent_dir else None
        self.skill_paths = tuple(os.fspath(path) for path in (skill_paths or ()))
        self.extension_paths = tuple(os.fspath(path) for path in (extension_paths or ()))
        self.environment = dict(environment or {})
        self._redaction_secrets = frozenset(
            value
            for key, value in self.environment.items()
            if is_secret_environment_key(key) and isinstance(value, str) and value
        )
        self.command_timeout = command_timeout
        self._process_factory = process_factory or self._default_process_factory
        self._event_buffer_size = event_buffer_size

        self.process: _Process | None = None
        self.state = PiProcessState.CREATED
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.last_error: str | None = None

        self._reader_task: asyncio.Task[Any] | None = None
        self._stderr_task: asyncio.Task[Any] | None = None
        self._wait_task: asyncio.Task[Any] | None = None
        self._write_lock = asyncio.Lock()
        self._pending_responses: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._events: deque[PiEvent] = deque(maxlen=event_buffer_size)
        self._next_cursor = 0
        self._meaningful_cursor = 0
        self._last_meaningful_at: float | None = None
        self._seen_event_identities: deque[str] = deque(maxlen=256)
        self._seen_status_fingerprints: deque[str] = deque(maxlen=64)
        self._last_payload_fingerprint: str | None = None
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._closed = False

    @staticmethod
    async def _default_process_factory(
        *args: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> _Process:
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=_RPC_STREAM_LIMIT,
        )

    @property
    def is_running(self) -> bool:
        """Whether the underlying process is currently alive."""

        return self.process is not None and self.process.returncode is None and not self._closed

    @property
    def returncode(self) -> int | None:
        return self.process.returncode if self.process is not None else None

    @property
    def event_cursor(self) -> int:
        """Cursor of the most recently observed event."""

        return self._next_cursor

    @property
    def meaningful_event_cursor(self) -> int:
        """Cursor of the most recent meaningful event, or zero."""

        return self._meaningful_cursor

    @property
    def last_meaningful_at(self) -> float | None:
        return self._last_meaningful_at

    def command_line(self) -> list[str]:
        """Build the immutable command used to launch Pi."""

        args = [*self.executable, "--mode", "rpc"]
        if self.session_dir:
            args.extend(["--session-dir", self.session_dir])
        if self.session_path:
            args.extend(["--session", self.session_path])
        if self.name:
            args.extend(["--name", self.name])
        if self.provider:
            args.extend(["--provider", self.provider])
        if self.model:
            args.extend(["--model", self.model])
        for path in self.extension_paths:
            args.extend(["--extension", path])
        for path in self.skill_paths:
            args.extend(["--skill", path])
        return args

    async def start(self) -> Self:
        """Launch Pi and start background stdout/stderr/wait loops."""

        if self.is_running:
            return self
        if self._reader_task and not self._reader_task.done():
            raise PiRpcError("Pi RPC adapter is already starting")

        self._closed = False
        args = self.command_line()
        try:
            environment = _child_environment()
            environment.update(self.environment)
            if self.agent_dir:
                environment["PI_CODING_AGENT_DIR"] = self.agent_dir
            try:
                process = self._process_factory(*args, cwd=self.cwd, env=environment)
            except TypeError as exc:
                # Keep simple test doubles and older adapters that only accept
                # ``cwd`` usable while the built-in factory receives env.
                if "env" not in str(exc):
                    raise
                process = self._process_factory(*args, cwd=self.cwd)
            if asyncio.iscoroutine(process) or isinstance(process, Awaitable):
                process = await process
            self.process = process
            # The child received the credentials; avoid retaining them in the
            # long-lived adapter object after process creation.
            self.environment.clear()
        except FileNotFoundError as exc:
            self.state = PiProcessState.EXITED
            self.last_error = f"Pi executable not found: {_command_display(self.executable)}"
            raise PiRpcError(self.last_error) from exc
        except OSError as exc:
            self.state = PiProcessState.EXITED
            self.last_error = (
                "Failed to start Pi: "
                f"{safe_error_summary(exc, secrets=self._redaction_secrets)}"
            )
            raise PiRpcError(self.last_error) from exc
        except Exception as exc:  # noqa: BLE001
            self.state = PiProcessState.EXITED
            self.last_error = (
                "Failed to start Pi: "
                f"{safe_error_summary(exc, secrets=self._redaction_secrets)}"
            )
            raise PiRpcError(self.last_error) from exc

        self.started_at = time.time()
        self.ended_at = None
        self.state = PiProcessState.RUNNING
        self._reader_task = asyncio.create_task(self._reader_loop(), name=f"pi-rpc-reader-{self.task_id}")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name=f"pi-rpc-stderr-{self.task_id}")
        self._wait_task = asyncio.create_task(self._wait_loop(), name=f"pi-rpc-wait-{self.task_id}")
        return self

    async def _wait_loop(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            code = await process.wait()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug("Pi wait loop failed for %s: %s", self.task_id, exc)
            return
        if self._closed:
            return
        self.ended_at = time.time()
        self.state = PiProcessState.EXITED
        self.last_error = None if code == 0 else f"Pi exited with code {code}"
        await self._record_event(
            {
                "type": "process_exit",
                "returncode": code,
                "task_id": self.task_id,
            },
            force_meaningful=True,
        )
        self._fail_pending(PiRpcError(self.last_error or "Pi process exited"))

    async def _reader_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while not self._closed:
                line = await process.stdout.readline()
                if not line:
                    break
                if isinstance(line, str):
                    raw = line
                else:
                    raw = line.decode("utf-8", errors="replace")
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await self._record_event(
                        {
                            "type": "rpc_error",
                            "error": "invalid_json",
                            "message": "Pi emitted an invalid JSONL event",
                        },
                        force_meaningful=True,
                    )
                    continue
                if not isinstance(payload, dict):
                    await self._record_event(
                        {
                            "type": "rpc_error",
                            "error": "invalid_event",
                            "message": "Pi RPC event must be a JSON object",
                        },
                        force_meaningful=True,
                    )
                    continue
                await self._handle_event(payload)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Pi stdout reader crashed for %s", self.task_id)
            await self._record_event(
                {
                    "type": "rpc_error",
                    "error": "reader_failed",
                    "message": safe_error_summary(exc),
                },
                force_meaningful=True,
            )

    async def _stderr_loop(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while not self._closed:
                line = await process.stderr.readline()
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                line = line.rstrip("\r\n")
                if line:
                    self._stderr_lines.append(
                        safe_error_summary(
                            line,
                            secrets=self._redaction_secrets,
                            max_length=512,
                        )
                    )
                    logger.debug("Pi[%s] emitted stderr", self.task_id)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug("Pi stderr reader failed for %s: %s", self.task_id, exc)

    async def _handle_event(self, payload: dict[str, Any]) -> None:
        payload = sanitize_value(payload, secrets=self._redaction_secrets)
        event_id = payload.get("id")
        if payload.get("type") == "response" and isinstance(event_id, str):
            future = self._pending_responses.pop(event_id, None)
            if future is not None and not future.done():
                future.set_result(payload)

        await self._record_event(payload)

    async def _record_event(
        self,
        payload: dict[str, Any],
        *,
        force_meaningful: bool = False,
    ) -> PiEvent:
        payload = sanitize_value(payload, secrets=self._redaction_secrets)
        self._next_cursor += 1
        meaningful = force_meaningful or self._is_meaningful(payload)
        event = PiEvent(
            cursor=self._next_cursor,
            received_at=time.time(),
            payload=payload,
            meaningful=meaningful,
        )
        self._events.append(event)
        if meaningful:
            self._meaningful_cursor = event.cursor
            self._last_meaningful_at = event.received_at
        return event

    def _is_meaningful(self, payload: dict[str, Any]) -> bool:
        payload_fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        duplicate_payload = payload_fingerprint == self._last_payload_fingerprint
        self._last_payload_fingerprint = payload_fingerprint

        event_type = payload.get("type")
        if event_type in {"heartbeat", "pong", "response", "ready", "connected"}:
            return False
        if duplicate_payload:
            return False

        # Pi may replay structured lifecycle events after reconnecting.  Only
        # dedupe those events when the protocol gives us a stable identity.
        # Identical text deltas without an event id remain meaningful because
        # they can be distinct new assistant output.
        identity = self._event_identity(payload)
        if identity is not None:
            if identity in self._seen_event_identities:
                return False
            self._seen_event_identities.append(identity)

        if event_type == "message_update":
            update = payload.get("assistantMessageEvent") or payload.get("messageEvent") or {}
            if not isinstance(update, dict):
                return False
            delta = update.get("delta")
            if isinstance(delta, str):
                return bool(delta)
            elif update.get("type") in {"text_delta", "thinking_delta"}:
                return False
            else:
                return bool(update)
        if event_type == "status":
            if not (payload.get("status") or payload.get("message")):
                return False
            fingerprint = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            if fingerprint in self._seen_status_fingerprints:
                return False
            self._seen_status_fingerprints.append(fingerprint)
            return True
        if event_type in _MEANINGFUL_TYPES:
            return True
        if isinstance(event_type, str) and any(
            token in event_type
            for token in ("agent", "turn", "tool", "artifact", "session", "compaction")
        ):
            return bool(payload)
        # Unknown events are useful to the task observer when they carry data,
        # but empty protocol envelopes are deliberately ignored.
        return bool(payload.get("data") or payload.get("message") or payload.get("error"))

    @staticmethod
    def _event_identity(payload: dict[str, Any]) -> str | None:
        """Return a stable Pi event id when one is present.

        ``response`` messages are handled separately and excluded before this
        function is called.  Their command correlation ids must not suppress a
        later unrelated response after the caller has already consumed it.
        """

        event_type = payload.get("type")
        if not isinstance(event_type, str):
            return None
        for field in ("eventId", "event_id", "sequence", "seq"):
            value = payload.get(field)
            if isinstance(value, (str, int)):
                return f"{event_type}:{field}:{value}"
        if event_type != "response":
            value = payload.get("id")
            if isinstance(value, (str, int)):
                return f"{event_type}:id:{value}"
        return None

    def _check_writable(self) -> _Process:
        process = self.process
        if process is None:
            raise PiRpcError("Pi process is not running")
        if process.returncode is not None or self._closed:
            raise PiRpcError(f"Pi process is not running (state={self.state.value})")
        if process.stdin is None or process.stdin.is_closing():
            raise PiRpcError("Pi process stdin is closed")
        return process

    async def _write_command(self, command: dict[str, Any]) -> None:
        process = self._check_writable()
        line = (json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                process.stdin.write(line)  # type: ignore[union-attr]
                await process.stdin.drain()  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                raise PiRpcError(
                    f"Failed to write Pi RPC command: {safe_error_summary(exc)}"
                ) from exc

    def _new_command_id(self, prefix: str = "cmd") -> str:
        return f"pi-{prefix}-{uuid.uuid4().hex}"

    async def send_command_nowait(self, command: dict[str, Any]) -> str:
        """Write a command and return immediately after the transport flush."""

        if not isinstance(command, dict):
            raise TypeError("command must be a dictionary")
        command_id = str(command.get("id") or self._new_command_id())
        outbound = {**command, "id": command_id}
        await self._write_command(outbound)
        return command_id

    async def send_prompt_nowait(
        self,
        message: str,
        *,
        images: Iterable[Any] | None = None,
        streaming_behavior: str | None = None,
    ) -> str:
        """Submit a prompt without waiting for Pi's agent response."""

        if not isinstance(message, str) or not message.strip():
            raise ValueError("Prompt message cannot be empty")
        command: dict[str, Any] = {"type": "prompt", "message": message}
        if images:
            command["images"] = list(images)
        if streaming_behavior:
            command["streamingBehavior"] = streaming_behavior
        return await self.send_command_nowait(command)

    async def _send_and_wait(
        self,
        command: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        command_id = str(command.get("id") or self._new_command_id("control"))
        outbound = {**command, "id": command_id}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_responses[command_id] = future
        try:
            await self._write_command(outbound)
        except Exception:
            self._pending_responses.pop(command_id, None)
            raise
        try:
            response = await asyncio.wait_for(
                future,
                timeout=self.command_timeout if timeout is None else timeout,
            )
        except asyncio.TimeoutError as exc:
            self._pending_responses.pop(command_id, None)
            raise PiRpcError(f"Timed out waiting for Pi command: {command.get('type')}") from exc
        if response.get("success") is False:
            raise PiRpcError(
                safe_error_summary(
                    response.get("error") or f"Pi rejected {command.get('type')}"
                )
            )
        return response

    async def steer(self, message: str) -> dict[str, Any]:
        """Ask Pi to steer the active turn with an additional instruction."""

        if not isinstance(message, str) or not message.strip():
            raise ValueError("Steer message cannot be empty")
        return await self._send_and_wait({"type": "steer", "message": message})

    async def abort(self) -> dict[str, Any]:
        """Abort the active Pi turn while keeping the process/session alive."""

        return await self._send_and_wait({"type": "abort"})

    async def cancel(self) -> dict[str, Any]:
        """Alias for :meth:`abort` used by task-level adapters."""

        return await self.abort()

    async def new_session(self, parent_session: str | None = None) -> dict[str, Any]:
        command: dict[str, Any] = {"type": "new_session"}
        if parent_session:
            command["parentSession"] = parent_session
        return await self._send_and_wait(command)

    async def switch_session(self, session_path: str | os.PathLike[str]) -> dict[str, Any]:
        return await self._send_and_wait(
            {"type": "switch_session", "sessionPath": os.fspath(session_path)}
        )

    async def get_state(self) -> dict[str, Any]:
        response = await self._send_and_wait({"type": "get_state"})
        return response.get("data", response)

    async def get_commands(self) -> list[Any]:
        response = await self._send_and_wait({"type": "get_commands"})
        data = response.get("data", response)
        return data.get("commands", []) if isinstance(data, dict) else []

    async def get_tree(self) -> dict[str, Any]:
        response = await self._send_and_wait({"type": "get_tree"})
        data = response.get("data", response)
        return data if isinstance(data, dict) else {"value": data}

    async def fork(self, entry_id: str) -> dict[str, Any]:
        if not entry_id:
            raise ValueError("entry_id cannot be empty")
        response = await self._send_and_wait({"type": "fork", "entryId": entry_id})
        data = response.get("data", response)
        return data if isinstance(data, dict) else {"value": data}

    def drain_events(
        self,
        *,
        after_cursor: int = 0,
        meaningful_only: bool = False,
        limit: int | None = None,
    ) -> list[PiEvent]:
        """Return buffered events newer than ``after_cursor`` without waiting."""

        events = [event for event in self._events if event.cursor > after_cursor]
        if meaningful_only:
            events = [event for event in events if event.meaningful]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            events = events[-limit:] if limit else []
        return events

    async def wait_for_event(self, timeout: float | None = None) -> PiEvent | None:
        """Wait for the next event, useful for tests or an optional notifier."""

        target = self._next_cursor
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        while True:
            events = self.drain_events(after_cursor=target, limit=1)
            if events:
                return events[0]
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                await asyncio.sleep(min(0.05, remaining))
            else:
                await asyncio.sleep(0.05)

    def snapshot(self) -> dict[str, Any]:
        """Return a polling-friendly state snapshot with no I/O or waits."""

        return {
            "task_id": self.task_id,
            "state": self.state.value,
            "running": self.is_running,
            "returncode": self.returncode,
            "session_path": self.session_path,
            "session_dir": self.session_dir,
            "cwd": self.cwd,
            "name": self.name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "event_cursor": self.event_cursor,
            "oldest_event_cursor": self._events[0].cursor if self._events else None,
            "meaningful_event_cursor": self.meaningful_event_cursor,
            "last_meaningful_at": self.last_meaningful_at,
            "pending_commands": list(self._pending_responses),
            "last_error": self.last_error,
        }

    def recovery_snapshot(self) -> dict[str, Any]:
        """Return enough metadata for a registry to decide whether to resume."""

        result = self.snapshot()
        result.update(
            {
                "recoverable": bool(self.session_path),
                "command": self.command_line(),
                "session_exists": bool(self.session_path and Path(self.session_path).exists()),
            }
        )
        return result

    @staticmethod
    def inspect_session(session_path: str | os.PathLike[str]) -> dict[str, Any]:
        """Inspect a persisted Pi JSONL session without starting a process."""

        path = Path(session_path).expanduser()
        result: dict[str, Any] = {
            "session_path": str(path),
            "exists": path.is_file(),
            "size": path.stat().st_size if path.is_file() else 0,
            "modified_at": path.stat().st_mtime if path.is_file() else None,
            "header": None,
            "error": None,
        }
        if not path.is_file():
            return result
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        header = json.loads(line)
                        result["header"] = header
                        break
        except (OSError, json.JSONDecodeError) as exc:
            result["error"] = safe_error_summary(exc)
        return result

    @classmethod
    async def from_session(
        cls,
        session_path: str | os.PathLike[str],
        **kwargs: Any,
    ) -> Self:
        """Create and start a fresh adapter bound to an existing session file."""

        adapter = cls(session_path=session_path, **kwargs)
        await adapter.start()
        return adapter

    async def resume(self, session_path: str | os.PathLike[str] | None = None) -> Self:
        """Restart this adapter (or rebind it) for recovery after process exit."""

        if session_path is not None:
            self.session_path = os.fspath(session_path)
        if self.is_running:
            return self
        await self._stop_background_tasks()
        self.process = None
        self.state = PiProcessState.CREATED
        self.last_error = None
        self._closed = False
        await self.start()
        return self

    async def _stop_background_tasks(self) -> None:
        current = asyncio.current_task()
        for task_name in ("_reader_task", "_stderr_task", "_wait_task"):
            task = getattr(self, task_name)
            if task is None or task is current or task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(self, task_name, None)

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending_responses.values():
            if not future.done():
                future.set_exception(error)
        self._pending_responses.clear()

    async def terminate(self, *, kill_after: float = 5.0) -> None:
        """Terminate the process and background loops without task-level timeouts."""

        self._closed = True
        self._fail_pending(PiRpcError("Pi RPC adapter terminated"))
        process = self.process
        if process is not None and process.returncode is None:
            try:
                if process.stdin is not None and not process.stdin.is_closing():
                    try:
                        process.stdin.write_eof()
                        await process.stdin.drain()
                    except Exception:  # noqa: BLE001
                        pass
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=kill_after)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            except (ProcessLookupError, OSError):
                pass
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to terminate Pi process %s: %s", self.task_id, exc)
        await self._stop_background_tasks()
        self.ended_at = self.ended_at or time.time()
        self.state = PiProcessState.TERMINATED
        self.process = None
        self._redaction_secrets = frozenset()

    async def close(self) -> None:
        """Alias for :meth:`terminate`."""

        await self.terminate()
