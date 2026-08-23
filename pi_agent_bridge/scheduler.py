"""Background supervision for independent Pi RPC workers.

The scheduler owns process lifecycles, while :class:`TaskRegistry` owns the
durable task state. Public methods perform one bounded operation only: no
method waits for a Pi turn to finish and there is deliberately no task or
idle timeout. The observer merely records snapshots at a configurable
interval; the main model remains free to poll those snapshots whenever it is
ready.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import shutil
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from .artifacts import discover_workspace_artifacts
from .models import TaskRecord, TaskStatus
from .registry import InvalidTaskTransition, TaskNotFoundError, TaskRegistry
from .rpc import PiRpcAdapter, PiRpcError
from .runtime import ExecutableCommand, PiRuntimeAdapter
from .security import safe_error_summary, sanitize_value
from .worker import PiWorkerConfig, WorkerConfigFactory, with_agent_dir

logger = logging.getLogger(__name__)

AdapterFactory = Callable[..., PiRpcAdapter]
ProcessProbe = Callable[[int], bool]
_ACTIVE_STATUSES = frozenset({TaskStatus.RUNNING, TaskStatus.NEEDS_USER_DECISION})
_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


def _is_process_alive(process_id: int) -> bool:
    """Return whether an OS process currently owns ``process_id``.

    A positive result only means the PID still exists.  The recovery adapter
    must still authenticate/reconnect to its transport before it is adopted;
    PIDs can be reused after an AstrBot restart.
    """

    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class TaskScheduler:
    """Supervise one Pi process per task without occupying an AstrBot turn."""

    def __init__(
        self,
        registry: TaskRegistry,
        *,
        workspace_root: str | Path,
        adapter_factory: AdapterFactory = PiRpcAdapter,
        executable: ExecutableCommand = "pi",
        runtime_adapter: PiRuntimeAdapter | None = None,
        provider: str | None = None,
        model: str | None = None,
        environment: dict[str, str] | None = None,
        poll_interval_seconds: int = 60,
        no_meaningful_event_limit: int = 3,
        max_concurrent_tasks: int = 4,
        command_timeout: float = 10.0,
        session_retention_hours: float | None = 24,
        session_root: str | Path | None = None,
        agent_root: str | Path | None = None,
        worker_config_factory: WorkerConfigFactory | None = None,
        process_probe: ProcessProbe = _is_process_alive,
        task_update_callback: Callable[[TaskRecord, Mapping[str, Any]], Awaitable[Any] | Any]
        | None = None,
    ) -> None:
        if poll_interval_seconds < 1:
            raise ValueError("poll_interval_seconds must be at least 1")
        if no_meaningful_event_limit < 1:
            raise ValueError("no_meaningful_event_limit must be at least 1")
        if max_concurrent_tasks < 1:
            raise ValueError("max_concurrent_tasks must be at least 1")
        if command_timeout <= 0:
            raise ValueError("command_timeout must be positive")
        if session_retention_hours is not None and session_retention_hours < 0:
            raise ValueError("session_retention_hours must be non-negative")

        self.registry = registry
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.session_root = (
            Path(session_root).expanduser().resolve(strict=False)
            if session_root is not None
            else self.workspace_root.parent / "sessions"
        )
        self.session_root.mkdir(parents=True, exist_ok=True)
        self.agent_root = (
            Path(agent_root).expanduser().resolve(strict=False)
            if agent_root is not None
            else self.workspace_root.parent / "agents"
        )
        self.agent_root.mkdir(parents=True, exist_ok=True)
        self.adapter_factory = adapter_factory
        self.executable = executable
        self.runtime_adapter = runtime_adapter
        self.provider = provider
        self.model = model
        self.environment = dict(environment or {})
        self.poll_interval_seconds = poll_interval_seconds
        self.no_meaningful_event_limit = no_meaningful_event_limit
        self.max_concurrent_tasks = max_concurrent_tasks
        self.command_timeout = command_timeout
        self.session_retention_hours = session_retention_hours
        self.process_probe = process_probe
        self.worker_config_factory = worker_config_factory
        self.task_update_callback = task_update_callback
        self._notified_task_states: set[tuple[str, str]] = set()

        self._adapters: dict[str, PiRpcAdapter] = {}
        self._observer_task: asyncio.Task[None] | None = None
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()
        self._submit_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._recovered = False
        self._resolved_executable: tuple[str, ...] | None = None

    async def _worker_config_for(
        self,
        task: TaskRecord,
        supplied: PiWorkerConfig | None = None,
    ) -> PiWorkerConfig:
        """Resolve one task's transient launch values without persisting secrets."""

        config = supplied
        if config is None and self.worker_config_factory is not None:
            result = self.worker_config_factory(task)
            if isinstance(result, Awaitable) or inspect.isawaitable(result):
                result = await result
            config = result
        if config is None:
            config = PiWorkerConfig(
                provider=self.provider,
                model=self.model,
                environment=self.environment,
            )
        if not isinstance(config, PiWorkerConfig):
            raise TypeError("worker_config_factory must return PiWorkerConfig")
        return with_agent_dir(config, self.agent_dir_for(task.task_id))

    async def _adapter_kwargs(
        self,
        task: TaskRecord,
        *,
        worker_config: PiWorkerConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        config = await self._worker_config_for(task, worker_config)
        kwargs.update(
            {
                "provider": config.provider,
                "model": config.model,
                "environment": dict(config.environment),
                "agent_dir": config.agent_dir,
                "skill_paths": config.skill_paths,
                "extension_paths": config.extension_paths,
            }
        )
        return kwargs

    @property
    def closed(self) -> bool:
        """Whether new workers are currently refused."""

        return self._closed

    def resolve_executable(self) -> ExecutableCommand:
        """Resolve the worker command once, immediately before it is needed.

        Resolving a plugin-owned Node/Pi bundle may legitimately fail on a
        machine where the bundle has not been installed yet.  Keeping that
        work out of plugin initialization means a normal AstrBot turn is
        never blocked or broken merely because no Pi task is being launched.
        The resulting error is instead captured in that task's structured
        result envelope by :class:`PiTaskService`.
        """

        if self.runtime_adapter is None:
            return self.executable
        if self._resolved_executable is None:
            self._resolved_executable = self.runtime_adapter.resolve_command()
        return self._resolved_executable

    async def start(self) -> None:
        """Start the lightweight observer loop, idempotently."""

        async with self._lifecycle_lock:
            if self._closed:
                self._recovered = False
            self._closed = False
            if self._observer_task is None or self._observer_task.done():
                self._observer_task = asyncio.create_task(
                    self._observe_loop(), name="pi-task-observer"
                )
        # Recovery is deliberately a separate phase from observation.  It is
        # idempotent for a scheduler instance and runs only after the durable
        # registry is open, so a plugin restart can reclaim session-backed
        # tasks before the first poll arrives.
        await self.recover_existing_tasks()

    async def shutdown(self, *, terminate_workers: bool = True) -> None:
        """Stop observation and terminate workers owned by this scheduler.

        This is a plugin-lifecycle operation, not a task timeout. A task that
        is merely quiet is never terminated here; only unloading the plugin or
        an explicit cancel/delete operation closes its worker.
        """

        async with self._lifecycle_lock:
            self._closed = True
            observer = self._observer_task
            self._observer_task = None

        if observer is not None and not observer.done():
            observer.cancel()
            try:
                await observer
            except asyncio.CancelledError:
                pass

        async with self._submit_lock:
            adapters = list(self._adapters.values())
            self._adapters.clear()
        if terminate_workers:
            await asyncio.gather(
                *(adapter.terminate() for adapter in adapters), return_exceptions=True
            )
            for adapter in adapters:
                self._mark_orphaned(
                    adapter.task_id,
                    "Pi worker stopped during scheduler shutdown",
                )
        else:
            # A host that can keep the parent process alive may detach workers
            # and let the next scheduler instance recover from session files.
            # We never pretend stdio can be reattached by PID.
            # Keep the durable state as ``running`` and preserve its PID.  The
            # next scheduler gets one chance to take over the transport; only
            # an unsuccessful takeover turns the task into ``orphaned``.
            return

    async def recover_existing_tasks(self) -> list[str]:
        """Best-effort takeover of persisted workers after plugin restart.

        Pi's standard RPC transport is a child-process stdin/stdout stream and
        has no attach-by-PID operation.  A transport adapter may opt in to
        ``take_over`` when it has a reconnectable worker endpoint.  The stock
        adapter cannot do that, so it safely retains the task and marks it
        ``orphaned`` rather than launching a second writer for the same Pi
        session.  A later explicit ``resume`` may start a fresh worker from
        the durable session file.

        Returns task ids successfully bound to a live adapter.  Repeated calls
        are safe and only inspect tasks that are not already owned by this
        scheduler instance.
        """

        async with self._recovery_lock:
            if self._recovered:
                return [task_id for task_id, adapter in self._adapters.items() if adapter.is_running]
            self._recovered = True

            if self.session_retention_hours is not None:
                self.cleanup_expired_tasks()

            candidates = self.registry.list_tasks(
                statuses=[
                    TaskStatus.RUNNING,
                    TaskStatus.NEEDS_USER_DECISION,
                ]
            )
            recovered: list[str] = []
            for task in candidates:
                if task.task_id in self._adapters:
                    continue
                if len(self._adapters) >= self.max_concurrent_tasks:
                    self._mark_orphaned(task.task_id, "recovery concurrency limit reached")
                    continue
                adapter = await self._recover_task(task)
                if adapter is not None:
                    recovered.append(task.task_id)
            return recovered

    async def _recover_task(self, task: TaskRecord) -> PiRpcAdapter | None:
        if task.process_id is None or not self.process_probe(task.process_id):
            self._mark_orphaned(task.task_id, "Pi worker is no longer alive after restart")
            return None

        adapter = await self._try_take_over(task)
        if adapter is not None:
            return adapter
        self._mark_orphaned(
            task.task_id,
            "Pi worker is alive but its RPC transport cannot be reattached",
        )
        return None

    async def _try_take_over(self, task: TaskRecord) -> PiRpcAdapter | None:
        """Ask a reconnect-capable adapter factory to reclaim a live worker."""

        take_over = getattr(self.adapter_factory, "take_over", None)
        if not callable(take_over):
            return None
        workspace = self.workspace_for(task.task_id, task.workspace)
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            takeover_kwargs = await self._adapter_kwargs(task, **{
                "task_id": task.task_id,
                "process_id": task.process_id,
                "session_id": task.session_id,
                "session_path": task.session_path,
                "cwd": workspace,
                "name": f"astrbot-{task.task_id[:8]}",
                "command_timeout": self.command_timeout,
            })
            # ``take_over`` is an optional extension point predating runtime
            # resolution. Keep its old call contract unless a runtime adapter
            # is explicitly configured.
            if self.runtime_adapter is not None:
                takeover_kwargs["executable"] = self.resolve_executable()
            result = _call_supported(take_over, takeover_kwargs)
            if inspect.isawaitable(result):
                result = await result
            if result is None or not getattr(result, "is_running", False):
                return None
            adapter = result
            self._adapters[task.task_id] = adapter
            state = await adapter.get_state()
            self.registry.update_runtime(
                task.task_id,
                session_id=_session_id(state) or task.session_id,
                session_path=_session_path(state) or task.session_path,
                process_id=_process_id(adapter) or task.process_id,
                workspace=str(workspace),
            )
            current = self.registry.get_task(task.task_id)
            if current.status is TaskStatus.ORPHANED:
                self.registry.transition_status(task.task_id, TaskStatus.RUNNING)
            return adapter
        except Exception:  # noqa: BLE001
            if "adapter" in locals():
                self._adapters.pop(task.task_id, None)
                await _terminate_quietly(adapter)
            logger.debug("Pi worker takeover failed for %s", task.task_id, exc_info=True)
            return None

    async def _restart_from_session(
        self,
        task: TaskRecord,
        *,
        worker_config: PiWorkerConfig | None = None,
    ) -> PiRpcAdapter | None:
        """Start a new isolated worker only after an explicit user resume."""

        if task.process_id is not None and self.process_probe(task.process_id):
            raise PiRpcError(
                "The original Pi worker is still alive but cannot be reattached"
            )
        session_path = task.session_path
        if not session_path:
            return None
        path = Path(session_path).expanduser()
        if not path.is_file():
            return None
        try:
            workspace = self.workspace_for(task.task_id, task.workspace)
            workspace.mkdir(parents=True, exist_ok=True)
            session_dir = self.session_dir_for(task.task_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            adapter_kwargs = await self._adapter_kwargs(
                task,
                worker_config=worker_config,
                **{
                    "task_id": task.task_id,
                    "executable": self.resolve_executable(),
                    "session_path": str(path),
                    "session_dir": str(session_dir),
                    "cwd": workspace,
                    "name": f"astrbot-{task.task_id[:8]}",
                    "command_timeout": self.command_timeout,
                },
            )
            adapter = _call_supported(self.adapter_factory, adapter_kwargs)
            await adapter.start()
            state = await adapter.get_state()
            self._adapters[task.task_id] = adapter
            self.registry.update_runtime(
                task.task_id,
                session_id=_session_id(state) or task.session_id,
                session_path=str(path),
                process_id=_process_id(adapter),
                workspace=str(workspace),
            )
            current = self.registry.get_task(task.task_id)
            return adapter
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if "adapter" in locals():
                await _terminate_quietly(adapter)
            logger.debug("Pi session restart failed for %s: %s", task.task_id, exc)
            return None

    def cleanup_expired_tasks(self, retention_hours: float | None = None) -> list[TaskRecord]:
        """Purge terminal metadata and task-owned files past retention."""

        hours = self.session_retention_hours if retention_hours is None else retention_hours
        if hours is None:
            return []
        expired = self.registry.purge_expired_tasks(hours)
        for task in expired:
            adapter = self._adapters.pop(task.task_id, None)
            if adapter is not None:
                self._schedule_termination(adapter)
            if task.workspace:
                self._remove_workspace(task.workspace)
            self._remove_owned_session(task)
            self._remove_agent_dir(task.task_id)
        return expired

    async def submit(
        self,
        task: TaskRecord,
        *,
        prompt: str,
        worker_config: PiWorkerConfig | None = None,
    ) -> TaskRecord:
        """Launch a worker and return after the prompt is accepted by stdin.

        Pi's response stream is consumed by its adapter in background tasks.
        The only waits here are process startup and short RPC acknowledgements;
        there is no wait for agent completion and no task-level timeout.
        """

        if self._closed:
            raise PiRpcError("Pi task scheduler is shut down")
        if task.status is not TaskStatus.QUEUED:
            raise PiRpcError(f"Task {task.task_id} is not queued")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt cannot be empty")

        # A scheduler may be used directly in tests or by a host that forgot
        # to call initialize(). Starting the observer is cheap and idempotent.
        await self.start()

        async with self._submit_lock:
            if self._closed:
                raise PiRpcError("Pi task scheduler is shut down")
            if task.task_id in self._adapters:
                raise PiRpcError(f"Task {task.task_id} already has a worker")
            active = sum(
                1 for adapter in self._adapters.values() if adapter.is_running
            )
            if active >= self.max_concurrent_tasks:
                raise PiRpcError("Pi task concurrency limit reached")

            workspace = self.workspace_for(task.task_id, task.workspace)
            workspace.mkdir(parents=True, exist_ok=True)
            session_dir = self.session_dir_for(task.task_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            adapter_kwargs = await self._adapter_kwargs(
                task,
                worker_config=worker_config,
                **{
                    "task_id": task.task_id,
                    "executable": self.resolve_executable(),
                    "session_dir": str(session_dir),
                    "cwd": workspace,
                    "name": f"astrbot-{task.task_id[:8]}",
                    "command_timeout": self.command_timeout,
                },
            )
            adapter = _call_supported(self.adapter_factory, adapter_kwargs)
            self._adapters[task.task_id] = adapter
            try:
                # Keep the submit lock until startup is complete so shutdown
                # cannot terminate a worker halfway through its handshake.
                await adapter.start()
                new_session = await adapter.new_session()
                state = await adapter.get_state()
                session_id = _session_id(state)
                session_path = (
                    _session_path(state)
                    or _session_path(new_session)
                    or getattr(adapter, "session_path", None)
                )
                self.registry.update_runtime(
                    task.task_id,
                    session_id=session_id,
                    session_path=str(session_path) if session_path else None,
                    process_id=_process_id(adapter),
                    workspace=str(workspace),
                )
                self.registry.transition_status(task.task_id, TaskStatus.RUNNING)
                await adapter.send_prompt_nowait(prompt.strip())
                return self.registry.get_task(task.task_id)
            except asyncio.CancelledError:
                self._adapters.pop(task.task_id, None)
                await _terminate_quietly(adapter)
                raise
            except Exception:
                self._adapters.pop(task.task_id, None)
                await _terminate_quietly(adapter)
                self._mark_failed_start(task.task_id)
                raise

    def _mark_failed_start(self, task_id: str) -> None:
        try:
            current = self.registry.get_task(task_id)
            if current.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                self.registry.transition_status(task_id, TaskStatus.FAILED)
        except (TaskNotFoundError, InvalidTaskTransition):
            # The task may have been deleted or completed concurrently.
            return

    def workspace_for(self, task_id: str, requested: str | Path | None = None) -> Path:
        """Normalize a task workspace without creating it.

        Relative paths are rooted under ``workspace_root``. Absolute paths are
        accepted so callers can delegate work in an existing project. A
        workspace that is already a regular file is rejected; directory
        creation happens only when the worker is actually launched.
        """

        if not task_id or Path(task_id).name != task_id:
            raise ValueError("task_id must be a simple non-empty identifier")
        candidate = Path(requested).expanduser() if requested else self.workspace_root / task_id
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        normalized = candidate.resolve(strict=False)
        if normalized.exists() and not normalized.is_dir():
            raise ValueError(f"workspace is not a directory: {normalized}")
        return normalized

    def session_dir_for(self, task_id: str) -> Path:
        """Return the plugin-owned native Pi session directory for a task."""

        if not task_id or Path(task_id).name != task_id:
            raise ValueError("task_id must be a simple non-empty identifier")
        return self.session_root / task_id

    def agent_dir_for(self, task_id: str) -> Path:
        """Return the isolated Pi agent configuration directory for a task."""

        if not task_id or Path(task_id).name != task_id:
            raise ValueError("task_id must be a simple non-empty identifier")
        return self.agent_root / task_id

    async def poll_task(
        self,
        task_id: str,
    ) -> TaskRecord:
        """Record one background observation; never wait for a Pi turn.

        This is the scheduler's only observation path.  It may make one
        bounded ``get_state`` RPC request and is responsible for advancing the
        no-progress state machine.  Main-model tools intentionally do not call
        this method: they read the already durable snapshot through
        :class:`PiTaskService` so chat turns cannot influence observation
        timing, overwrite events, or consume a worker RPC slot.
        """

        task = self.registry.get_task(task_id)
        if task.status in _TERMINAL_STATUSES:
            return task

        adapter = self._adapters.get(task_id)
        if adapter is None:
            if task.status is TaskStatus.RUNNING:
                try:
                    return self.registry.transition_status(task_id, TaskStatus.ORPHANED)
                except InvalidTaskTransition:
                    return self.registry.get_task(task_id)
            return task

        after_cursor = _cursor_value(task.event_cursor)
        events = adapter.drain_events(after_cursor=after_cursor)
        pi_state, state_error = await self._poll_pi_state(adapter)
        previous_snapshot = self.registry.get_latest_snapshot(task_id)
        state_changed = _pi_state_changed(
            previous_snapshot.payload if previous_snapshot is not None else None,
            pi_state,
        )
        # A manually inspected paused task can still expose fresh Pi state,
        # but an unchanged observation must not continue incrementing the
        # no-progress counter after the user-decision threshold was reached.
        if (
            task.status is TaskStatus.NEEDS_USER_DECISION
            and not events
            and not state_changed
            and state_error is None
        ):
            return task
        meaningful = any(event.meaningful for event in events) or state_changed
        snapshot = adapter.snapshot()
        snapshot["events"] = [event.as_dict() for event in events]
        snapshot["pi_state"] = pi_state
        if state_error is not None:
            snapshot["state_poll_error"] = state_error
        snapshot["artifacts"] = discover_workspace_artifacts(
            task.workspace or self.workspace_root / task_id
        )
        snapshot["phase"] = _phase(events, snapshot)
        finished = _agent_finished(events)
        failed = _worker_failed(events, snapshot)
        cursor = str(getattr(adapter, "event_cursor", task.event_cursor or "0"))
        updated, _, _ = self.registry.record_snapshot(
            task_id,
            snapshot,
            has_meaningful_event=meaningful,
            event_cursor=cursor,
            no_meaningful_event_limit=self.no_meaningful_event_limit,
        )
        self._store_new_artifacts(task_id, snapshot["artifacts"])

        if failed and updated.status in _ACTIVE_STATUSES:
            updated = self.registry.transition_status(task_id, TaskStatus.FAILED)
        elif finished and updated.status in _ACTIVE_STATUSES:
            updated = self.registry.transition_status(task_id, TaskStatus.COMPLETED)
        if updated.status in _TERMINAL_STATUSES:
            await self._release_terminal_worker(task_id, adapter)
        if updated.status in _TERMINAL_STATUSES | {TaskStatus.NEEDS_USER_DECISION}:
            await self._notify_task_update(updated, snapshot)
        return updated

    async def _notify_task_update(
        self, task: TaskRecord, snapshot: Mapping[str, Any]
    ) -> None:
        """Notify once for actionable terminal states, never for running progress."""

        if self.task_update_callback is None:
            return
        key = (task.task_id, task.status.value)
        if key in self._notified_task_states:
            return
        self._notified_task_states.add(key)
        result = self.task_update_callback(task, snapshot)
        if inspect.isawaitable(result):
            await result

    async def _poll_pi_state(
        self, adapter: PiRpcAdapter
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Request one bounded remote Pi state snapshot for an observation.

        ``get_state`` is a short RPC control call, never a wait for agent
        completion. If Pi cannot acknowledge it, preserve a sanitized error in
        the latest snapshot and let the normal no-progress state machine decide
        whether user intervention is needed.
        """

        try:
            state = await adapter.get_state()
        except Exception as exc:  # noqa: BLE001
            return None, safe_error_summary(exc)
        if isinstance(state, Mapping):
            return dict(sanitize_value(state)), None
        return {"value": sanitize_value(state)}, None

    async def follow_up(self, task_id: str, message: str) -> TaskRecord:
        """Inject an additional requirement through Pi's steer command."""

        if not isinstance(message, str) or not message.strip():
            raise ValueError("follow-up message cannot be empty")
        task = self.registry.get_task(task_id)
        if task.status not in {TaskStatus.RUNNING, TaskStatus.NEEDS_USER_DECISION}:
            raise PiRpcError(f"Cannot follow up task in {task.status.value} state")
        adapter = self._require_adapter(task_id)
        if task.status is TaskStatus.NEEDS_USER_DECISION:
            self.registry.resume_task(task_id)
        await adapter.steer(message.strip())
        return await self.poll_task(task_id)

    async def resume(self, task_id: str) -> TaskRecord:
        """Resume a logically paused task while preserving its worker/session."""

        task = self.registry.get_task(task_id)
        if task.status not in {
            TaskStatus.RUNNING,
            TaskStatus.NEEDS_USER_DECISION,
            TaskStatus.ORPHANED,
        }:
            raise PiRpcError(f"Cannot resume task in {task.status.value} state")
        adapter = self._adapters.get(task_id)
        if adapter is None or not adapter.is_running:
            adapter = await self._restart_from_session(task)
        if adapter is None:
            raise PiRpcError("Pi worker is unavailable for this task")
        if task.status in {TaskStatus.NEEDS_USER_DECISION, TaskStatus.ORPHANED}:
            self.registry.resume_task(task_id)
        await adapter.steer("Continue the task and report your next meaningful progress.")
        return await self.poll_task(task_id)

    async def cancel(self, task_id: str) -> TaskRecord:
        """Abort the active turn and mark the durable task cancelled."""

        task = self.registry.get_task(task_id)
        adapter = self._adapters.get(task_id)
        cancel_error: Exception | None = None
        if adapter is not None and adapter.is_running:
            try:
                await adapter.cancel()
            except Exception as exc:  # noqa: BLE001
                cancel_error = exc
            finally:
                # ``abort`` only ends the current Pi turn.  The RPC process is
                # task-owned and must also be closed so a cancelled task frees
                # its concurrency slot while its session/history remain durable.
                await self._release_terminal_worker(task_id, adapter)
        if task.status not in _TERMINAL_STATUSES:
            task = self.registry.transition_status(task_id, TaskStatus.CANCELLED)
        if cancel_error is not None:
            raise PiRpcError(f"Pi cancel acknowledgement failed: {safe_error_summary(cancel_error)}") from cancel_error
        return task

    async def _release_terminal_worker(
        self,
        task_id: str,
        adapter: PiRpcAdapter,
    ) -> None:
        """Detach and close a worker after terminal task state is durable."""

        async with self._submit_lock:
            current = self._adapters.get(task_id)
            if current is not adapter:
                return
            self._adapters.pop(task_id, None)
        await _terminate_quietly(adapter)

    async def delete(
        self,
        task_id: str,
        *,
        clean_workspace: bool = True,
        clean_session: bool = True,
    ) -> None:
        """Terminate a worker, remove metadata, and optionally remove files."""

        async with self._submit_lock:
            adapter = self._adapters.pop(task_id, None)
        if adapter is not None:
            await adapter.terminate()
        task = self.registry.get_task(task_id)
        self.registry.delete_task(task_id)
        if clean_workspace and task.workspace:
            self._remove_workspace(task.workspace)
        if clean_session:
            self._remove_owned_session(task)
        self._remove_agent_dir(task.task_id)

    def adapter(self, task_id: str) -> PiRpcAdapter | None:
        """Return a live adapter for diagnostics and host integrations."""

        return self._adapters.get(task_id)

    async def _observe_loop(self) -> None:
        try:
            while not self._closed:
                self.cleanup_expired_tasks()
                await asyncio.sleep(self.poll_interval_seconds)
                task_ids = [
                    task.task_id
                    for task in self.registry.list_tasks(statuses=[TaskStatus.RUNNING])
                    if task.task_id in self._adapters
                ]
                await asyncio.gather(
                    *(self._poll_safely(task_id) for task_id in task_ids),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            return

    async def _poll_safely(self, task_id: str) -> None:
        try:
            await self.poll_task(task_id)
        except (TaskNotFoundError, InvalidTaskTransition):
            return
        except Exception:  # noqa: BLE001
            logger.exception("Pi task observation failed for %s", task_id)

    def _schedule_termination(self, adapter: PiRpcAdapter) -> None:
        """Terminate a cleaned worker without blocking retention cleanup."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            _terminate_quietly(adapter),
            name=f"pi-cleanup-{adapter.task_id}",
        )

    def _require_adapter(self, task_id: str) -> PiRpcAdapter:
        adapter = self._adapters.get(task_id)
        if adapter is None or not adapter.is_running:
            raise PiRpcError("Pi worker is unavailable for this task")
        return adapter

    def _store_new_artifacts(self, task_id: str, items: list[dict[str, Any]]) -> None:
        known_paths = {artifact.path for artifact in self.registry.list_artifacts(task_id)}
        for item in items:
            path = item.get("path")
            if not isinstance(path, str) or path in known_paths:
                continue
            self.registry.add_artifact(task_id, **item)
            known_paths.add(path)

    def _remove_workspace(self, workspace: str) -> None:
        root = Path(workspace).expanduser().resolve(strict=False)
        allowed = self.workspace_root
        try:
            inside_root = root == allowed or root.is_relative_to(allowed)
        except AttributeError:  # pragma: no cover - Python < 3.9 compatibility
            inside_root = str(root).startswith(str(allowed))
        if inside_root and root != allowed:
            shutil.rmtree(root, ignore_errors=True)

    def _remove_task_sessions(self, task_id: str) -> None:
        """Remove only session files under the scheduler-owned session root."""

        session_dir = self.session_dir_for(task_id).resolve(strict=False)
        try:
            inside_root = session_dir.is_relative_to(self.session_root)
        except AttributeError:  # pragma: no cover - Python < 3.9 compatibility
            inside_root = str(session_dir).startswith(str(self.session_root))
        if inside_root and session_dir != self.session_root:
            shutil.rmtree(session_dir, ignore_errors=True)

    def _remove_agent_dir(self, task_id: str) -> None:
        """Remove only the task-owned Pi agent configuration directory."""

        agent_dir = self.agent_dir_for(task_id).resolve(strict=False)
        try:
            owned = agent_dir.is_relative_to(self.agent_root)
        except AttributeError:  # pragma: no cover
            owned = str(agent_dir).startswith(str(self.agent_root))
        if owned and agent_dir != self.agent_root:
            shutil.rmtree(agent_dir, ignore_errors=True)

    def _remove_owned_session(self, task: TaskRecord) -> None:
        """Remove a session only when it is inside plugin-owned storage."""

        if task.session_path:
            path = Path(task.session_path).expanduser().resolve(strict=False)
            try:
                owned = path.is_relative_to(self.session_root)
            except AttributeError:  # pragma: no cover - Python < 3.9 compatibility
                owned = str(path).startswith(str(self.session_root))
            if owned and path.is_file():
                try:
                    path.unlink()
                except OSError:
                    logger.debug("Failed to remove Pi session %s", path, exc_info=True)
                parent = path.parent
                if parent != self.session_root:
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
        self._remove_task_sessions(task.task_id)

    def _mark_orphaned(
        self,
        task_id: str,
        reason: str,
        *,
        clear_process: bool = True,
    ) -> TaskRecord | None:
        """Persist a meaningful recovery diagnostic without exposing an exception."""

        try:
            task = self.registry.get_task(task_id)
            if task.status in _TERMINAL_STATUSES:
                return task
            if task.status is TaskStatus.NEEDS_USER_DECISION:
                self.registry.record_snapshot(
                    task_id,
                    {
                        "phase": "needs_user_decision",
                        "recovery": {"message": reason},
                        "process_id": task.process_id,
                        "session_id": task.session_id,
                        "session_path": task.session_path,
                    },
                    has_meaningful_event=True,
                    event_cursor=task.event_cursor,
                    no_meaningful_event_limit=self.no_meaningful_event_limit,
                )
                return (
                    self.registry.detach_process(task.task_id)
                    if clear_process
                    else task
                )
            self.registry.record_snapshot(
                task_id,
                {
                    "phase": "orphaned",
                    "recovery": {"message": reason},
                    "process_id": task.process_id,
                    "session_id": task.session_id,
                    "session_path": task.session_path,
                },
                has_meaningful_event=True,
                event_cursor=task.event_cursor,
                no_meaningful_event_limit=self.no_meaningful_event_limit,
            )
            task = self.registry.get_task(task_id)
            if task.status is not TaskStatus.ORPHANED:
                task = self.registry.transition_status(task_id, TaskStatus.ORPHANED)
            return self.registry.detach_process(task.task_id) if clear_process else task
        except (TaskNotFoundError, InvalidTaskTransition):
            return None


def _process_id(adapter: PiRpcAdapter) -> int | None:
    process = getattr(adapter, "process", None)
    pid = getattr(process, "pid", None)
    return pid if isinstance(pid, int) else None


def _call_supported(callable_obj: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    """Call legacy adapter factories without forcing new optional keywords."""

    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return callable_obj(**kwargs)
    parameters = signature.parameters.values()
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return callable_obj(**kwargs)
    accepted = set(signature.parameters)
    return callable_obj(**{key: value for key, value in kwargs.items() if key in accepted})


async def _terminate_quietly(adapter: PiRpcAdapter) -> None:
    try:
        await adapter.terminate()
    except Exception:  # noqa: BLE001
        logger.debug("Failed to clean up Pi worker %s", adapter.task_id, exc_info=True)


def _session_id(state: Any) -> str | None:
    if not isinstance(state, dict):
        return None
    for key in ("sessionId", "session_id", "session", "id"):
        value = state.get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    nested = state.get("data")
    return _session_id(nested)


def _session_path(state: Any) -> str | None:
    """Extract a persisted native session path from Pi state responses."""

    if not isinstance(state, dict):
        return None
    for key in ("sessionPath", "session_path", "sessionFile", "session_file", "path"):
        value = state.get(key)
        if isinstance(value, (str, Path)) and str(value):
            return str(value)
    nested = state.get("data")
    return _session_path(nested)


def _cursor_value(value: str | None) -> int:
    try:
        return max(0, int(value or "0"))
    except (TypeError, ValueError):
        return 0


def _agent_finished(events: list[Any]) -> bool:
    return any(event.payload.get("type") == "agent_end" for event in events)


def _worker_failed(events: list[Any], snapshot: dict[str, Any]) -> bool:
    if snapshot.get("state") == "exited" and snapshot.get("returncode") not in {None, 0}:
        return True
    return any(event.payload.get("type") in {"rpc_error", "error"} for event in events)


def _phase(events: list[Any], snapshot: dict[str, Any]) -> str:
    if any(event.payload.get("type") == "agent_end" for event in events):
        return "completed"
    if snapshot.get("running"):
        return "working"
    return str(snapshot.get("state") or "unknown")


def _pi_state_changed(
    previous_snapshot: Mapping[str, Any] | None,
    current_state: Mapping[str, Any] | None,
) -> bool:
    """Detect meaningful Pi state transitions without counting volatile fields."""

    if not current_state:
        return False
    previous_state = (
        previous_snapshot.get("pi_state")
        if isinstance(previous_snapshot, Mapping)
        else None
    )
    if not isinstance(previous_state, Mapping):
        # The first state is an observation baseline, not an event. Counting
        # it as progress would turn a configured three-empty-poll threshold
        # into four polls for an otherwise silent worker.
        return False
    volatile = {"messageCount", "pendingMessageCount"}
    current_comparable = {
        key: value for key, value in current_state.items() if key not in volatile
    }
    previous_comparable = {
        key: value for key, value in previous_state.items() if key not in volatile
    }
    return current_comparable != previous_comparable
