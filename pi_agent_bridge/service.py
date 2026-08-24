"""Short, structured operations exposed to AstrBot's main model."""

from __future__ import annotations

import asyncio
import json
import inspect
import uuid
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from .context import build_pi_prompt
from .models import ArtifactRecord, TaskRecord, TaskStatus
from .registry import InvalidTaskTransition, TaskNotFoundError, TaskRegistry
from .scheduler import TaskScheduler
from .security import safe_error_summary


RECENT_SESSION_CHARS = 50_000


class PiTaskService:
    """Facade whose methods never wait for a Pi task to finish.

    ``create_task`` persists the request and schedules worker startup on the
    event loop, then immediately returns a JSON-compatible envelope. Polling,
    follow-ups, cancellation, and inspection are all bounded operations whose
    errors stay in the envelope for the host model to interpret.
    """

    def __init__(self, registry: TaskRegistry, scheduler: TaskScheduler) -> None:
        self.registry = registry
        self.scheduler = scheduler
        self._launch_tasks: set[asyncio.Task[None]] = set()
        self._launch_task_ids: dict[asyncio.Task[None], str] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def create_task(
        self,
        *,
        owner_key: str,
        task: str,
        context: Mapping[str, Any] | None = None,
        persona: str | None = None,
        media_references: list[str] | None = None,
        workspace: str | None = None,
        prepare_context: Any | None = None,
    ) -> dict[str, Any]:
        """Create a task and return before Pi performs any agent work."""

        if self._closed:
            return self.error("task_create", "Pi task service is shut down")
        if not isinstance(owner_key, str) or not owner_key.strip():
            return self.error("task_create", "owner_key cannot be empty")
        if not isinstance(task, str) or not task.strip():
            return self.error("task_create", "Task text cannot be empty")

        task_id: str | None = None
        try:
            await self.scheduler.start()
            task_id = str(uuid.uuid4())
            normalized_workspace = self.scheduler.workspace_for(task_id, workspace)
            prepared_context = dict(context or {})
            prepared_persona = persona
            prepared_media = list(media_references or [])
            if callable(prepare_context):
                prepared = prepare_context(task_id, normalized_workspace)
                if inspect.isawaitable(prepared):
                    prepared = await prepared
                if isinstance(prepared, Mapping):
                    prepared_context.update(dict(prepared))
                    if isinstance(prepared.get("persona"), str):
                        prepared_persona = prepared["persona"]
                    refs = prepared.get("media_references")
                    if isinstance(refs, list | tuple):
                        prepared_media = [str(item) for item in refs if str(item)]
            self.registry.create_task(
                owner_key=owner_key.strip(),
                prompt=task.strip(),
                context=prepared_context,
                workspace=str(normalized_workspace),
                task_id=task_id,
            )
            prompt = build_pi_prompt(
                task,
                context_snapshot=prepared_context,
                persona=prepared_persona,
                media_references=prepared_media,
            )
            launch = asyncio.create_task(
                self._launch(task_id, prompt),
                name=f"pi-task-launch-{task_id}",
            )
            self._launch_tasks.add(launch)
            self._launch_task_ids[launch] = task_id
            launch.add_done_callback(self._discard_launch)
            return self.envelope("task_create", task_id)
        except Exception as exc:  # noqa: BLE001
            # If validation failed after insertion, leave a durable failed task
            # rather than leaking a queued row that can never start.
            if task_id is not None:
                self._mark_start_failure(task_id, exc)
            return self.error("task_create", safe_error_summary(exc), task_id=task_id)

    async def _launch(self, task_id: str, prompt: str) -> None:
        """Start a worker in the background, outside the tool invocation."""

        try:
            await self.scheduler.submit(self.registry.get_task(task_id), prompt=prompt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._mark_start_failure(task_id, exc)

    def _discard_launch(self, launch: asyncio.Task[None]) -> None:
        self._launch_tasks.discard(launch)
        self._launch_task_ids.pop(launch, None)

    def _mark_start_failure(self, task_id: str, _exc: Exception) -> None:
        try:
            current = self.registry.get_task(task_id)
            if current.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                self.registry.transition_status(task_id, TaskStatus.FAILED)
        except (TaskNotFoundError, InvalidTaskTransition):
            return

    async def shutdown(self) -> None:
        """Cancel pending launches and finalize tasks that never started."""

        self._closed = True
        pending = list(self._launch_tasks)
        pending_ids = [
            self._launch_task_ids.get(launch)
            for launch in pending
            if not launch.done()
        ]
        self._launch_tasks.clear()
        self._launch_task_ids.clear()
        for launch in pending:
            if not launch.done():
                launch.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task_id in pending_ids:
            if task_id:
                self._mark_cancelled_before_start(task_id)

    def _mark_cancelled_before_start(self, task_id: str) -> None:
        """Finalize a queued task whose background launch was cancelled."""

        try:
            current = self.registry.get_task(task_id)
            if current.status is TaskStatus.QUEUED:
                self.registry.transition_status(task_id, TaskStatus.FAILED)
        except (TaskNotFoundError, InvalidTaskTransition):
            return

    async def close(self) -> None:
        """Alias for :meth:`shutdown` used by host lifecycle adapters."""

        await self.shutdown()

    async def poll(self, task_id: str) -> dict[str, Any]:
        """Ask Pi for one bounded state observation without returning its output."""

        try:
            await self.scheduler.poll_task(task_id)
            return self.envelope("task_poll", task_id)
        except Exception as exc:  # noqa: BLE001
            return self.error("task_poll", safe_error_summary(exc), task_id=task_id)

    async def follow_up(self, task_id: str, message: str) -> dict[str, Any]:
        return await self._run_scheduler_operation(
            "task_follow_up", task_id, self.scheduler.follow_up, message
        )

    async def resume(self, task_id: str) -> dict[str, Any]:
        return await self._run_scheduler_operation("task_resume", task_id, self.scheduler.resume)

    async def cancel(self, task_id: str) -> dict[str, Any]:
        return await self._run_scheduler_operation("task_cancel", task_id, self.scheduler.cancel)

    async def delete(self, task_id: str) -> dict[str, Any]:
        try:
            await self.scheduler.delete(task_id)
            return self.ok("task_delete", task_id=task_id, status="deleted")
        except Exception as exc:  # noqa: BLE001
            return self.error("task_delete", safe_error_summary(exc), task_id=task_id)

    def status(self, task_id: str) -> dict[str, Any]:
        try:
            return self.envelope("task_status", task_id)
        except Exception as exc:  # noqa: BLE001
            return self.error("task_status", safe_error_summary(exc), task_id=task_id)

    def result(
        self, task_id: str, *, offset: int = -1, limit: int = 100
    ) -> dict[str, Any]:
        """Compatibility alias for the bounded native session reader."""

        result = self.read(task_id, cursor=offset, limit=limit)
        result["operation"] = "task_result"
        return result

    def read(
        self, task_id: str, *, cursor: int = -1, limit: int = 100
    ) -> dict[str, Any]:
        """Read the recent native session tail without semantic processing."""

        try:
            if cursor < -1 or limit < 1:
                raise ValueError("cursor must be -1 or non-negative and limit must be positive")
            task = self.registry.get_task(task_id)
            if cursor == -1:
                text, start_byte, total_bytes = _read_session_tail(
                    task.session_path, max_chars=RECENT_SESSION_CHARS
                )
                result = self.envelope("task_read", task_id)
                result["session_text"] = text
                result["progress"]["read"] = {
                    "mode": "recent_tail",
                    "max_chars": RECENT_SESSION_CHARS,
                    "returned_chars": len(text),
                    "start_byte": start_byte,
                    "end_byte": total_bytes,
                    "total_bytes": total_bytes,
                    "has_more": start_byte > 0,
                    "source": "pi_native_session_jsonl",
                    "session_path": task.session_path,
                }
                return result
            return self._read_full_page(task_id, cursor=cursor, limit=limit)
        except Exception as exc:  # noqa: BLE001
            return self.error("task_read", safe_error_summary(exc), task_id=task_id)

    def read_full(self, task_id: str, *, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        """Read a page of complete native JSONL lines without a size cap."""

        try:
            if cursor < 0 or limit < 1:
                raise ValueError("cursor must be non-negative and limit must be positive")
            return self._read_full_page(task_id, cursor=cursor, limit=limit)
        except Exception as exc:  # noqa: BLE001
            return self.error("task_read_full", safe_error_summary(exc), task_id=task_id)

    def _read_full_page(self, task_id: str, *, cursor: int, limit: int) -> dict[str, Any]:
        task = self.registry.get_task(task_id)
        lines = _read_session_lines(task.session_path, cursor=cursor, limit=limit)
        next_cursor = cursor + len(lines)
        result = self.envelope("task_read_full", task_id)
        result["session_lines"] = lines
        result["progress"]["read"] = {
            "mode": "full_lines",
            "cursor": cursor,
            "next_cursor": next_cursor,
            "returned": len(lines),
            "has_more": _session_has_more(task.session_path, next_cursor),
            "source": "pi_native_session_jsonl",
            "session_path": task.session_path,
        }
        return result

    def list_tasks(self, owner_key: str | None = None) -> dict[str, Any]:
        try:
            tasks = [
                self._task_dict(task)
                for task in self.registry.list_tasks(owner_key=owner_key)
            ]
            return self.ok("task_list", status="ok", progress={"resource_type": "async_task", "count": len(tasks), "tasks": tasks})
        except Exception as exc:  # noqa: BLE001
            return self.error("task_list", safe_error_summary(exc))

    def session_list(self, owner_key: str | None = None) -> dict[str, Any]:
        try:
            sessions = []
            for task in self.registry.list_tasks(owner_key=owner_key):
                if task.session_id or task.workspace:
                    sessions.append(
                        {
                            "task_id": task.task_id,
                            "session_id": task.session_id,
                            "session_path": task.session_path,
                            "status": task.status.value,
                            "workspace": task.workspace,
                        }
                    )
            return self.ok("session_list", status="ok", progress={"resource_type": "async_task_session", "count": len(sessions), "sessions": sessions})
        except Exception as exc:  # noqa: BLE001
            return self.error("session_list", safe_error_summary(exc))

    def session_inspect(self, task_id: str) -> dict[str, Any]:
        result = self.status(task_id)
        result["operation"] = "session_inspect"
        return result

    async def session_resume(self, task_id: str) -> dict[str, Any]:
        result = await self.resume(task_id)
        result["operation"] = "session_resume"
        return result

    async def session_delete(self, task_id: str) -> dict[str, Any]:
        result = await self.delete(task_id)
        result["operation"] = "session_delete"
        return result

    def artifact_inspect(self, task_id: str) -> dict[str, Any]:
        try:
            task = self.registry.get_task(task_id)
            return self.ok(
                "artifact_inspect",
                task_id=task_id,
                status=task.status.value,
                artifacts=[
                    self._artifact_dict(item)
                    for item in self.registry.list_artifacts(task_id)
                ],
            )
        except Exception as exc:  # noqa: BLE001
            return self.error("artifact_inspect", safe_error_summary(exc), task_id=task_id)

    async def _run_scheduler_operation(
        self,
        operation: str,
        task_id: str,
        method: Any,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            await method(task_id, *args, **kwargs)
            return self.envelope(operation, task_id)
        except Exception as exc:  # noqa: BLE001
            return self.error(operation, safe_error_summary(exc), task_id=task_id)

    def envelope(self, operation: str, task_id: str) -> dict[str, Any]:
        """Return task control metadata without Pi event content."""

        task = self.registry.get_task(task_id)
        return self.ok(
            operation,
            task_id=task.task_id,
            status=task.status.value,
            progress={
                "task": self._task_dict(task),
                "session": {
                    "session_id": task.session_id,
                    "session_path": task.session_path,
                    "source": "pi_native_session_jsonl",
                },
                "observer": self.scheduler.observation_info(task_id),
            },
        )

    def ok(
        self,
        operation: str,
        *,
        task_id: str | None = None,
        status: str | None = None,
        progress: Mapping[str, Any] | None = None,
        content: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "ok": True,
            "operation": operation,
            "task_id": task_id,
            "status": status,
            "progress": dict(progress or {}),
            "content": list(content or []),
            "artifacts": list(artifacts or []),
            "error": None,
        }

    def error(
        self,
        operation: str,
        message: str,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "ok": False,
            "operation": operation,
            "task_id": task_id,
            "status": None,
            "progress": {},
            "content": [],
            "artifacts": [],
            "error": {"type": "pi_task_error", "message": str(message)},
        }

    @staticmethod
    def dump(result: Mapping[str, Any]) -> str:
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _task_dict(task: TaskRecord) -> dict[str, Any]:
        return {
            "resource_type": "async_task",
            "task_id": task.task_id,
            "owner_key": task.owner_key,
            "status": task.status.value,
            "session_id": task.session_id,
            "session_path": task.session_path,
            "workspace": task.workspace,
            "event_cursor": task.event_cursor,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "finished_at": task.finished_at,
        }

    @staticmethod
    def _artifact_dict(item: ArtifactRecord) -> dict[str, Any]:
        return {
            "artifact_id": item.artifact_id,
            "kind": item.kind,
            "path": item.path,
            "mime_type": item.mime_type,
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
            "metadata": item.metadata,
        }


def _read_session_tail(
    session_path: str | None, *, max_chars: int
) -> tuple[str, int, int]:
    if not session_path:
        return "", 0, 0
    path = Path(session_path).expanduser()
    if not path.is_file():
        return "", 0, 0
    total_bytes = path.stat().st_size
    window_start = max(0, total_bytes - max_chars * 4)
    with path.open("rb") as stream:
        stream.seek(window_start)
        raw = stream.read()
    decoded = raw.decode("utf-8", errors="replace")
    text = decoded[-max_chars:]
    start_byte = total_bytes - len(text.encode("utf-8"))
    return text, max(0, start_byte), total_bytes


def _read_session_lines(
    session_path: str | None, *, cursor: int, limit: int
) -> list[str]:
    if not session_path:
        return []
    path = Path(session_path).expanduser()
    if not path.is_file():
        return []
    lines: list[str] = []
    with path.open("rb") as stream:
        for index, line in enumerate(stream):
            if index < cursor:
                continue
            if len(lines) >= limit:
                break
            lines.append(line.decode("utf-8", errors="replace"))
    return lines


def _session_has_more(session_path: str | None, cursor: int) -> bool:
    if not session_path:
        return False
    path = Path(session_path).expanduser()
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        for index, _line in enumerate(stream):
            if index >= cursor:
                return True
    return False
