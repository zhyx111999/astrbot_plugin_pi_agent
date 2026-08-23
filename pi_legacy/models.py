"""Data models for the Pi legacy compatibility route."""

from dataclasses import dataclass, field


@dataclass
class SessionInfo:
    """Summary of a pi session."""

    session_id: str
    session_file: str
    cwd: str
    session_name: str | None = None
    message_count: int = 0
    thinking_level: str | None = None
    is_streaming: bool = False
    timestamp: str | None = None
    first_message_snippet: str | None = None


@dataclass
class UIRequest:
    """A pending extension UI request from pi."""

    local_id: int
    request_id: str
    method: str
    title: str = ""
    message: str = ""
    options: list = field(default_factory=list)
    prefill: str = ""
    timeout_ms: int | None = None
    created_at: float = 0.0

    def to_dict(self) -> dict:
        """Return a dictionary representation for logging or serialization."""
        return {
            "local_id": self.local_id,
            "request_id": self.request_id,
            "method": self.method,
            "title": self.title,
            "message": self.message,
            "options": self.options,
            "prefill": self.prefill,
            "timeout_ms": self.timeout_ms,
            "created_at": self.created_at,
        }
