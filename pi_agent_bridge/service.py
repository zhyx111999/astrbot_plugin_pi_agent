"""Short, structured operations exposed to AstrBot's main model."""

from __future__ import annotations

import asyncio
import json
import inspect
import uuid
from collections.abc import Mapping
from typing import Any

from .context import build_pi_prompt
from .models import ArtifactRecord, TaskRecord, TaskStatus
from .registry import InvalidTaskTransition, TaskNotFoundError, TaskRegistry
from .scheduler import TaskScheduler
from .security import safe_error_summary


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
        self._reported_snapshot_ids: dict[str, int] = {}
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

    def _mark_start_failure(self, task_id: str, exc: Exception) -> None:
        try:
            current = self.registry.get_task(task_id)
        except TaskNotFoundError:
            return
        snapshot = {
            "phase": "failed_to_start",
            "error": {
                "type": type(exc).__name__,
                "message": safe_error_summary(exc),
            },
        }
        try:
            self.registry.record_snapshot(
                task_id,
                snapshot,
                has_meaningful_event=True,
                event_cursor=current.event_cursor,
            )
        except TaskNotFoundError:
            return
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
            if current.status is not TaskStatus.QUEUED:
                return
            self.registry.record_snapshot(
                task_id,
                {
                    "phase": "cancelled_before_start",
                    "error": {
                        "type": "service_shutdown",
                        "message": "Pi worker launch was cancelled before startup",
                    },
                },
                has_meaningful_event=True,
                event_cursor=current.event_cursor,
            )
            self.registry.transition_status(task_id, TaskStatus.FAILED)
        except (TaskNotFoundError, InvalidTaskTransition):
            return

    async def close(self) -> None:
        """Alias for :meth:`shutdown` used by host lifecycle adapters."""

        await self.shutdown()

    async def poll(self, task_id: str) -> dict[str, Any]:
        """Read the newest observer snapshot without touching the worker.

        The scheduler alone performs remote state requests and progresses the
        three-observation no-progress counter.  Keeping this tool local makes
        it safe for the main model to call repeatedly while it is handling
        unrelated chat turns.
        """

        try:
            return self.envelope("task_poll", task_id, report_newness=True)
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
            self._reported_snapshot_ids.pop(task_id, None)
            return self.ok("task_delete", task_id=task_id, status="deleted")
        except Exception as exc:  # noqa: BLE001
            return self.error("task_delete", safe_error_summary(exc), task_id=task_id)

    def status(self, task_id: str) -> dict[str, Any]:
        try:
            return self.envelope("task_status", task_id)
        except Exception as exc:  # noqa: BLE001
            return self.error("task_status", safe_error_summary(exc), task_id=task_id)

    def result(self, task_id: str) -> dict[str, Any]:
        try:
            return self.envelope("task_result", task_id, include_all_events=True)
        except Exception as exc:  # noqa: BLE001
            return self.error("task_result", safe_error_summary(exc), task_id=task_id)

    def list_tasks(self, owner_key: str | None = None) -> dict[str, Any]:
        try:
            tasks = [
                self._task_dict(task)
                for task in self.registry.list_tasks(owner_key=owner_key)
            ]
            return self.ok("task_list", status="ok", progress={"tasks": tasks})
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
            return self.ok("session_list", status="ok", progress={"sessions": sessions})
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

    def envelope(
        self,
        operation: str,
        task_id: str,
        *,
        include_all_events: bool = False,
        report_newness: bool = False,
    ) -> dict[str, Any]:
        task = self.registry.get_task(task_id)
        snapshot = self.registry.get_latest_snapshot(task_id)
        snapshot_payload = snapshot.payload if snapshot else {}
        events = snapshot_payload.get("events", []) if isinstance(snapshot_payload, dict) else []
        has_new_meaningful_event = False
        if report_newness and snapshot is not None:
            previous_id = self._reported_snapshot_ids.get(task_id)
            has_new_meaningful_event = (
                snapshot.has_meaningful_event and previous_id != snapshot.snapshot_id
            )
            self._reported_snapshot_ids[task_id] = snapshot.snapshot_id
        return self.ok(
            operation,
            task_id=task.task_id,
            status=task.status.value,
            has_new_meaningful_event=has_new_meaningful_event,
            progress={
                "task": self._task_dict(task),
                "snapshot": snapshot_payload,
                "no_meaningful_event_count": task.no_meaningful_event_count,
            },
            content=_content_blocks(events, include_all=include_all_events),
            artifacts=[
                self._artifact_dict(item)
                for item in self.registry.list_artifacts(task_id)
            ],
        )

    def ok(
        self,
        operation: str,
        *,
        task_id: str | None = None,
        status: str | None = None,
        has_new_meaningful_event: bool = False,
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
            "has_new_meaningful_event": has_new_meaningful_event,
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
            "has_new_meaningful_event": False,
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


def _content_blocks(events: Any, *, include_all: bool) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []
    blocks: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        update = payload.get("assistantMessageEvent") or payload.get("messageEvent")
        text = update.get("delta") if isinstance(update, dict) else payload.get("text")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
        elif include_all and payload.get("type") in {"agent_end", "tool_end", "artifact"}:
            blocks.append({"type": "event", "event": payload})
    return blocks
