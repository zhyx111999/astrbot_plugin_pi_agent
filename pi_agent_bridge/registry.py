"""SQLite task registry for the current Pi Agent plugin generation.

This generation uses a fresh task database schema. It contains only task
identity, lifecycle, worker, session, and workspace metadata.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import TaskRecord, TaskStatus


class TaskRegistryError(RuntimeError):
    """Base error for task registry operations."""


class TaskNotFoundError(TaskRegistryError):
    """Raised when a task does not exist."""


class InvalidTaskTransition(TaskRegistryError):
    """Raised when a lifecycle transition is invalid."""


_TERMINAL = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.NEEDS_USER_DECISION,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.ORPHANED,
        }
    ),
    TaskStatus.NEEDS_USER_DECISION: frozenset(
        {TaskStatus.RUNNING, TaskStatus.ORPHANED, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.ORPHANED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _encode(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _decode(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {}


class TaskRegistry:
    """Thread-safe, fresh-schema task metadata store."""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database).expanduser()
        if str(self.database) != ":memory:":
            self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.database), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "TaskRegistry":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def journal_mode(self) -> str:
        with self._lock:
            return str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                owner_key TEXT NOT NULL,
                session_origin TEXT NOT NULL,
                status TEXT NOT NULL,
                prompt TEXT NOT NULL,
                context_json TEXT NOT NULL,
                session_id TEXT,
                session_path TEXT,
                process_id INTEGER,
                workspace TEXT,
                event_cursor TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_owner_status
                ON tasks(owner_key, status);
            CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at);
            """
        )

    @contextmanager
    def _write_transaction(self):
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    @staticmethod
    def _task(row: sqlite3.Row | None) -> TaskRecord:
        if row is None:
            raise TaskNotFoundError("task not found")
        return TaskRecord(
            task_id=row["task_id"],
            owner_key=row["owner_key"],
            session_origin=row["session_origin"],
            status=TaskStatus(row["status"]),
            prompt=row["prompt"],
            context=_decode(row["context_json"]),
            session_id=row["session_id"],
            session_path=row["session_path"],
            process_id=row["process_id"],
            workspace=row["workspace"],
            event_cursor=row["event_cursor"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=row["finished_at"],
        )

    def create_task(
        self,
        *,
        owner_key: str,
        session_origin: str,
        prompt: str,
        context: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        session_path: str | None = None,
        process_id: int | None = None,
        workspace: str | None = None,
        task_id: str | None = None,
        status: TaskStatus = TaskStatus.QUEUED,
    ) -> TaskRecord:
        if not owner_key or not session_origin or not prompt:
            raise ValueError("owner_key, session_origin, and prompt are required")
        task_id = task_id or str(uuid.uuid4())
        status = TaskStatus(status)
        now = _utc_now()
        finished_at = now if status in _TERMINAL else None
        with self._lock, self._write_transaction():
            self._connection.execute(
                """
                INSERT INTO tasks(
                    task_id, owner_key, session_origin, status, prompt, context_json,
                    session_id, session_path, process_id, workspace, event_cursor,
                    created_at, updated_at, finished_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id,
                    owner_key,
                    session_origin,
                    status.value,
                    prompt,
                    _encode(context),
                    session_id,
                    session_path,
                    process_id,
                    workspace,
                    None,
                    now,
                    now,
                    finished_at,
                ),
            )
            return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        with self._lock:
            return self._task(
                self._connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    def list_tasks(
        self,
        *,
        owner_key: str | None = None,
        statuses: Sequence[TaskStatus | str] | None = None,
    ) -> list[TaskRecord]:
        clauses: list[str] = []
        values: list[Any] = []
        if owner_key is not None:
            clauses.append("owner_key=?")
            values.append(owner_key)
        if statuses:
            normalized = [TaskStatus(status).value for status in statuses]
            clauses.append(f"status IN ({','.join('?' for _ in normalized)})")
            values.extend(normalized)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM tasks{where} ORDER BY updated_at DESC", values
            ).fetchall()
            return [self._task(row) for row in rows]

    def transition_status(self, task_id: str, status: TaskStatus | str) -> TaskRecord:
        target = TaskStatus(status)
        with self._lock, self._write_transaction():
            current = self.get_task(task_id)
            if target != current.status and target not in _TRANSITIONS[current.status]:
                raise InvalidTaskTransition(
                    f"cannot move {current.status.value} task to {target.value}"
                )
            now = _utc_now()
            self._connection.execute(
                "UPDATE tasks SET status=?, updated_at=?, finished_at=? WHERE task_id=?",
                (target.value, now, now if target in _TERMINAL else None, task_id),
            )
            return self.get_task(task_id)

    def resume_task(self, task_id: str) -> TaskRecord:
        with self._lock, self._write_transaction():
            task = self.get_task(task_id)
            if task.status not in {TaskStatus.NEEDS_USER_DECISION, TaskStatus.ORPHANED}:
                raise InvalidTaskTransition(f"cannot resume {task.status.value} task")
            self._connection.execute(
                "UPDATE tasks SET status=?, updated_at=?, finished_at=NULL WHERE task_id=?",
                (TaskStatus.RUNNING.value, _utc_now(), task_id),
            )
            return self.get_task(task_id)

    def update_runtime(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        session_path: str | None = None,
        process_id: int | None = None,
        workspace: str | None = None,
        event_cursor: str | None = None,
    ) -> TaskRecord:
        fields: list[str] = []
        values: list[Any] = []
        for name, value in (
            ("session_id", session_id),
            ("session_path", session_path),
            ("process_id", process_id),
            ("workspace", workspace),
            ("event_cursor", event_cursor),
        ):
            if value is not None:
                fields.append(f"{name}=?")
                values.append(value)
        if not fields:
            return self.get_task(task_id)
        with self._lock, self._write_transaction():
            self.get_task(task_id)
            fields.append("updated_at=?")
            values.extend([_utc_now(), task_id])
            self._connection.execute(
                f"UPDATE tasks SET {','.join(fields)} WHERE task_id=?", values
            )
            return self.get_task(task_id)

    def update_event_cursor(self, task_id: str, event_cursor: str) -> TaskRecord:
        return self.update_runtime(task_id, event_cursor=event_cursor)

    def detach_process(self, task_id: str) -> TaskRecord:
        with self._lock, self._write_transaction():
            self.get_task(task_id)
            self._connection.execute(
                "UPDATE tasks SET process_id=NULL, updated_at=? WHERE task_id=?",
                (_utc_now(), task_id),
            )
            return self.get_task(task_id)

    def delete_task(self, task_id: str) -> None:
        with self._lock, self._write_transaction():
            self.get_task(task_id)
            self._connection.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))

    def purge_expired_tasks(
        self,
        retention_hours: float,
        *,
        now: datetime | None = None,
        statuses: Sequence[TaskStatus | str] | None = None,
    ) -> list[TaskRecord]:
        if retention_hours < 0:
            raise ValueError("retention_hours must be non-negative")
        selected = [TaskStatus(status).value for status in (statuses or _TERMINAL)]
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)
        placeholders = ",".join("?" for _ in selected)
        with self._lock, self._write_transaction():
            rows = self._connection.execute(
                f"SELECT * FROM tasks WHERE status IN ({placeholders}) "
                "AND finished_at IS NOT NULL AND finished_at <= ? ORDER BY finished_at ASC",
                [*selected, cutoff.astimezone(timezone.utc).isoformat(timespec="milliseconds")],
            ).fetchall()
            tasks = [self._task(row) for row in rows]
            if tasks:
                self._connection.executemany(
                    "DELETE FROM tasks WHERE task_id=?", [(task.task_id,) for task in tasks]
                )
            return tasks


__all__ = [
    "InvalidTaskTransition",
    "TaskNotFoundError",
    "TaskRegistry",
    "TaskRegistryError",
]
