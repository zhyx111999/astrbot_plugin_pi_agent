"""SQLite-backed task state for the asynchronous Pi bridge.

The registry deliberately contains no process or AstrBot code.  It is a small
transactional state store that can be used by a worker supervisor and by short
polling calls independently of the chat-agent turn that created a task.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import ArtifactRecord, SnapshotRecord, TaskRecord, TaskStatus


class TaskRegistryError(RuntimeError):
    """Base error for invalid registry operations."""


class TaskNotFoundError(TaskRegistryError):
    """Raised when an operation references an unknown task."""


class InvalidTaskTransition(TaskRegistryError):
    """Raised when a state transition is not valid for the durable model."""


_TERMINAL = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
}
_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.RUNNING: {
        TaskStatus.NEEDS_USER_DECISION,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.ORPHANED,
    },
    TaskStatus.NEEDS_USER_DECISION: {
        TaskStatus.RUNNING,
        TaskStatus.ORPHANED,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    },
    TaskStatus.ORPHANED: {
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _decode(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {"value": decoded}


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


class TaskRegistry:
    """Thread-safe SQLite registry with WAL and short atomic transactions."""

    def __init__(self, database: str | Path):
        self.database = Path(database).expanduser()
        if str(self.database) != ":memory:":
            self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.database),
            check_same_thread=False,
            isolation_level=None,
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

    @property
    def journal_mode(self) -> str:
        """Return the configured SQLite journal mode for diagnostics."""
        with self._lock:
            return str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def __enter__(self) -> "TaskRegistry":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                owner_key TEXT NOT NULL,
                session_origin TEXT,
                status TEXT NOT NULL,
                prompt TEXT NOT NULL,
                context_json TEXT NOT NULL,
                session_id TEXT,
                session_path TEXT,
                process_id INTEGER,
                workspace TEXT,
                event_cursor TEXT,
                no_meaningful_event_count INTEGER NOT NULL DEFAULT 0,
                latest_snapshot_id INTEGER,
                latest_snapshot_fingerprint TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_owner_status ON tasks(owner_key, status);
            CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at);
            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                event_cursor TEXT,
                payload_json TEXT NOT NULL,
                has_meaningful_event INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(task_id, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_task_created ON snapshots(task_id, snapshot_id DESC);
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                path TEXT,
                mime_type TEXT,
                size_bytes INTEGER,
                sha256 TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_task_created ON artifacts(task_id, artifact_id DESC);
            """
        )
        # ``session_path`` was added after the first bridge prototype.  Keep
        # existing task databases usable by applying the tiny additive
        # migration at open time instead of requiring a destructive reset.
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "session_origin" not in columns:
            self._connection.execute("ALTER TABLE tasks ADD COLUMN session_origin TEXT")
        self._migrate_task_identities()
        # Existing databases may have been compacted by earlier bridge versions.
        # New observations are retained until task retention removes the task so
        # AstrBot can read a Pi session by event cursor without reconstruction.
        self._connection.execute(
            "UPDATE tasks SET latest_snapshot_id = "
            "(SELECT MAX(snapshot_id) FROM snapshots WHERE snapshots.task_id = tasks.task_id) "
            "WHERE EXISTS (SELECT 1 FROM snapshots WHERE snapshots.task_id = tasks.task_id)"
        )
        self._connection.execute(
            "UPDATE tasks SET latest_snapshot_fingerprint = "
            "(SELECT fingerprint FROM snapshots WHERE snapshots.snapshot_id = tasks.latest_snapshot_id) "
            "WHERE latest_snapshot_id IS NOT NULL"
        )

    def _migrate_task_identities(self) -> None:
        """Separate legacy session origins from stable task-owner identities."""

        rows = self._connection.execute(
            "SELECT task_id, owner_key, session_origin FROM tasks"
        ).fetchall()
        for row in rows:
            origin = row["session_origin"] or row["owner_key"]
            owner = row["owner_key"]
            if row["session_origin"] is None:
                self._connection.execute(
                    "UPDATE tasks SET session_origin=? WHERE task_id=?",
                    (origin, row["task_id"]),
                )
            if owner.count(":") == 2:
                platform, message_type, sender = owner.split(":", 2)
                if message_type == "FriendMessage" and sender:
                    owner = f"{platform}:{sender}"
                elif message_type == "GroupMessage":
                    owner = f"legacy:{row['task_id']}"
                self._connection.execute(
                    "UPDATE tasks SET owner_key=? WHERE task_id=?",
                    (owner, row["task_id"]),
                )


    @contextmanager
    def _write_transaction(self):
        """Serialize a state transition across registry instances/processes."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _task(self, row: sqlite3.Row | None) -> TaskRecord:
        if row is None:
            raise TaskNotFoundError("task not found")
        return TaskRecord(
            task_id=row["task_id"], owner_key=row["owner_key"],
            session_origin=row["session_origin"] or row["owner_key"],
            status=TaskStatus(row["status"]),
            prompt=row["prompt"], context=_decode(row["context_json"]), session_id=row["session_id"],
            session_path=row["session_path"],
            process_id=row["process_id"], workspace=row["workspace"], event_cursor=row["event_cursor"],
            no_meaningful_event_count=row["no_meaningful_event_count"], latest_snapshot_id=row["latest_snapshot_id"],
            latest_snapshot_fingerprint=row["latest_snapshot_fingerprint"], created_at=row["created_at"],
            updated_at=row["updated_at"], finished_at=row["finished_at"],
        )

    def create_task(
        self, *, owner_key: str, session_origin: str | None = None, prompt: str, context: Mapping[str, Any] | None = None,
        session_id: str | None = None, session_path: str | None = None,
        process_id: int | None = None, workspace: str | None = None,
        task_id: str | None = None, status: TaskStatus = TaskStatus.QUEUED,
    ) -> TaskRecord:
        task_id = task_id or str(uuid.uuid4())
        now = _utc_now()
        normalized_status = TaskStatus(status)
        finished_at = now if normalized_status in _TERMINAL else None
        with self._lock, self._write_transaction():
            self._connection.execute(
                "INSERT INTO tasks(task_id,owner_key,session_origin,status,prompt,context_json,session_id,session_path,process_id,workspace,created_at,updated_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, owner_key, session_origin or owner_key, normalized_status.value, prompt, _json(context), session_id, session_path, process_id, workspace, now, now, finished_at),
            )
            return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        with self._lock:
            return self._task(self._connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())

    def list_tasks(self, *, owner_key: str | None = None, statuses: Sequence[TaskStatus | str] | None = None) -> list[TaskRecord]:
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
            rows = self._connection.execute(f"SELECT * FROM tasks{where} ORDER BY updated_at DESC", values).fetchall()
            return [self._task(row) for row in rows]

    def transition_status(self, task_id: str, status: TaskStatus | str) -> TaskRecord:
        target = TaskStatus(status)
        with self._lock, self._write_transaction():
            current = self.get_task(task_id)
            if target != current.status and target not in _TRANSITIONS[current.status]:
                raise InvalidTaskTransition(f"cannot move {current.status.value} task to {target.value}")
            now = _utc_now()
            finished = now if target in _TERMINAL else None
            self._connection.execute("UPDATE tasks SET status=?, updated_at=?, finished_at=? WHERE task_id=?", (target.value, now, finished, task_id))
            return self.get_task(task_id)

    def resume_task(self, task_id: str) -> TaskRecord:
        with self._lock, self._write_transaction():
            current = self.get_task(task_id)
            if current.status not in {TaskStatus.NEEDS_USER_DECISION, TaskStatus.ORPHANED}:
                raise InvalidTaskTransition(f"cannot resume {current.status.value} task")
            now = _utc_now()
            self._connection.execute("UPDATE tasks SET status=?, no_meaningful_event_count=0, updated_at=?, finished_at=NULL WHERE task_id=?", (TaskStatus.RUNNING.value, now, task_id))
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
        with self._lock, self._write_transaction():
            self.get_task(task_id)
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
            if fields:
                fields.append("updated_at=?")
                values.extend([_utc_now(), task_id])
                self._connection.execute(f"UPDATE tasks SET {','.join(fields)} WHERE task_id=?", values)
            return self.get_task(task_id)

    def update_event_cursor(self, task_id: str, event_cursor: str) -> TaskRecord:
        """Advance the worker event cursor without creating a snapshot."""
        return self.update_runtime(task_id, event_cursor=event_cursor)

    def detach_process(self, task_id: str) -> TaskRecord:
        """Clear the transient PID binding while retaining session metadata."""

        with self._lock, self._write_transaction():
            self.get_task(task_id)
            self._connection.execute(
                "UPDATE tasks SET process_id=NULL, updated_at=? WHERE task_id=?",
                (_utc_now(), task_id),
            )
            return self.get_task(task_id)

    def record_snapshot(self, task_id: str, payload: Mapping[str, Any], *, has_meaningful_event: bool, event_cursor: str | None = None, no_meaningful_event_limit: int = 3) -> tuple[TaskRecord, SnapshotRecord | None, bool]:
        """Persist a raw Pi observation and atomically advance its cursor.

        ``has_meaningful_event`` and ``no_meaningful_event_limit`` remain in
        the storage API for database compatibility. They never infer a task
        state: only Pi lifecycle events and explicit control operations may
        change a task's status.
        """
        if no_meaningful_event_limit < 1:
            raise ValueError("no_meaningful_event_limit must be positive")
        fingerprint = _fingerprint({"payload": payload, "event_cursor": event_cursor}) if event_cursor is not None else _fingerprint(payload)
        with self._lock, self._write_transaction():
            current = self.get_task(task_id)
            now = _utc_now()
            row = self._connection.execute(
                "SELECT * FROM snapshots WHERE task_id=? AND snapshot_id=?",
                (task_id, current.latest_snapshot_id),
            ).fetchone() if current.latest_snapshot_id is not None else None
            inserted = row is None or row["fingerprint"] != fingerprint
            if inserted:
                cursor = self._connection.execute(
                    "INSERT INTO snapshots(task_id,event_cursor,payload_json,has_meaningful_event,fingerprint,created_at) VALUES(?,?,?,?,?,?)",
                    (task_id, event_cursor, _json(payload), int(has_meaningful_event), fingerprint, now),
                )
                snapshot_id = int(cursor.lastrowid)
                snapshot = SnapshotRecord(snapshot_id, task_id, event_cursor, dict(payload), has_meaningful_event, fingerprint, now)
                self._connection.execute(
                    "UPDATE tasks SET latest_snapshot_id=?, latest_snapshot_fingerprint=? WHERE task_id=?",
                    (snapshot_id, fingerprint, task_id),
                )
            else:
                snapshot_id = int(row["snapshot_id"])
                if event_cursor is not None:
                    self._connection.execute(
                        "UPDATE snapshots SET event_cursor=?, created_at=? WHERE snapshot_id=?",
                        (event_cursor, now, snapshot_id),
                    )
                    row = self._connection.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
                snapshot = SnapshotRecord(snapshot_id, task_id, row["event_cursor"], _decode(row["payload_json"]), bool(row["has_meaningful_event"]), row["fingerprint"], row["created_at"])
            count = 0 if has_meaningful_event else current.no_meaningful_event_count + 1
            self._connection.execute(
                "UPDATE tasks SET event_cursor=COALESCE(?,event_cursor), no_meaningful_event_count=?, latest_snapshot_id=?, latest_snapshot_fingerprint=?, updated_at=? WHERE task_id=?",
                (event_cursor, count, snapshot_id, fingerprint, now, task_id),
            )
            return self.get_task(task_id), snapshot, inserted

    def get_latest_snapshot(self, task_id: str) -> SnapshotRecord | None:
        with self._lock:
            task = self.get_task(task_id)
            if task.latest_snapshot_id is None:
                return None
            row = self._connection.execute(
                "SELECT * FROM snapshots WHERE task_id=? AND snapshot_id=?",
                (task_id, task.latest_snapshot_id),
            ).fetchone()
            if row is None:
                return None
            return SnapshotRecord(
                row["snapshot_id"], task_id, row["event_cursor"],
                _decode(row["payload_json"]), bool(row["has_meaningful_event"]),
                row["fingerprint"], row["created_at"],
            )

    def list_snapshots(self, task_id: str) -> list[SnapshotRecord]:
        """Return every retained raw observation for a task in creation order."""

        with self._lock:
            self.get_task(task_id)
            rows = self._connection.execute(
                "SELECT * FROM snapshots WHERE task_id=? ORDER BY snapshot_id ASC",
                (task_id,),
            ).fetchall()
            return [
                SnapshotRecord(
                    row["snapshot_id"],
                    task_id,
                    row["event_cursor"],
                    _decode(row["payload_json"]),
                    bool(row["has_meaningful_event"]),
                    row["fingerprint"],
                    row["created_at"],
                )
                for row in rows
            ]

    def add_artifact(self, task_id: str, *, kind: str, path: str | None = None, mime_type: str | None = None, size_bytes: int | None = None, sha256: str | None = None, metadata: Mapping[str, Any] | None = None) -> ArtifactRecord:
        with self._lock, self._write_transaction():
            self.get_task(task_id)
            now = _utc_now()
            rowid = self._connection.execute("INSERT INTO artifacts(task_id,kind,path,mime_type,size_bytes,sha256,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (task_id, kind, path, mime_type, size_bytes, sha256, _json(metadata), now)).lastrowid
            return self.get_artifact(int(rowid))

    def get_artifact(self, artifact_id: int) -> ArtifactRecord:
        with self._lock:
            row = self._connection.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if row is None:
                raise TaskRegistryError("artifact not found")
            return ArtifactRecord(row["artifact_id"], row["task_id"], row["kind"], row["path"], row["mime_type"], row["size_bytes"], row["sha256"], _decode(row["metadata_json"]), row["created_at"])

    def list_artifacts(self, task_id: str) -> list[ArtifactRecord]:
        with self._lock:
            self.get_task(task_id)
            rows = self._connection.execute("SELECT artifact_id FROM artifacts WHERE task_id=? ORDER BY artifact_id", (task_id,)).fetchall()
            return [self.get_artifact(int(row["artifact_id"])) for row in rows]

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
        """Delete terminal task rows older than the configured retention.

        The records are returned before deletion so the scheduler can remove
        task-owned workspaces and session files after the metadata transaction
        commits.  Active, paused, and orphaned tasks are never selected by
        default; retention is storage cleanup, not a task timeout.
        """

        if retention_hours < 0:
            raise ValueError("retention_hours must be non-negative")
        normalized = [
            TaskStatus(status).value
            for status in (statuses or tuple(_TERMINAL))
        ]
        if not normalized:
            return []
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)
        cutoff_text = cutoff.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        placeholders = ",".join("?" for _ in normalized)
        with self._lock, self._write_transaction():
            rows = self._connection.execute(
                f"SELECT * FROM tasks WHERE status IN ({placeholders}) "
                "AND finished_at IS NOT NULL AND finished_at <= ? "
                "ORDER BY finished_at ASC",
                [*normalized, cutoff_text],
            ).fetchall()
            records = [self._task(row) for row in rows]
            if records:
                self._connection.executemany(
                    "DELETE FROM tasks WHERE task_id=?",
                    [(record.task_id,) for record in records],
                )
            return records

