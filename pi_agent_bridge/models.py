"""Durable contracts for isolated Pi tasks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    """Lifecycle states managed by the Pi task bridge."""

    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_USER_DECISION = "needs_user_decision"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """One persisted Pi task and its original reply destination."""

    task_id: str
    owner_key: str
    session_origin: str
    status: TaskStatus
    prompt: str
    context: dict[str, Any]
    session_id: str | None
    session_path: str | None
    process_id: int | None
    workspace: str | None
    event_cursor: str | None
    created_at: str
    updated_at: str
    finished_at: str | None
