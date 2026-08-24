"""Data contracts used by the persistent Pi task registry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    """Durable task states understood by the bridge."""

    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_USER_DECISION = "needs_user_decision"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    owner_key: str
    status: TaskStatus
    prompt: str
    context: dict[str, Any]
    session_id: str | None
    process_id: int | None
    workspace: str | None
    event_cursor: str | None
    no_meaningful_event_count: int
    latest_snapshot_id: int | None
    latest_snapshot_fingerprint: str | None
    created_at: str
    updated_at: str
    finished_at: str | None
    session_path: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    snapshot_id: int
    task_id: str
    event_cursor: str | None
    payload: dict[str, Any]
    has_meaningful_event: bool
    fingerprint: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: int
    task_id: str
    kind: str
    path: str | None
    mime_type: str | None
    size_bytes: int | None
    sha256: str | None
    metadata: dict[str, Any]
    created_at: str
