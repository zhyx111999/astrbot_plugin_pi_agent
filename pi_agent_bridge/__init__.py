"""Independent Pi Agent runtime adapters used by the task-oriented plugin.

The ``pi_legacy`` package preserves the administrator-only interactive
compatibility route. This package adapts Pi's public ``--mode rpc`` JSONL
interface without modifying Pi or delivering messages into AstrBot chats.
"""

from .rpc import (
    PiEvent,
    PiProcessState,
    PiRpcAdapter,
    PiRpcError,
)
from .models import ArtifactRecord, SnapshotRecord, TaskRecord, TaskStatus
from .registry import InvalidTaskTransition, TaskNotFoundError, TaskRegistry, TaskRegistryError
from .astrbot_adapter import (
    AstrBotAdapter,
    AstrBotAdapterError,
    UnsupportedAstrBotCapability,
    WakeMainAgent,
)
from .scheduler import TaskScheduler
from .service import PiTaskService
from .runtime import (
    NODE_VERSION,
    PI_PACKAGE_NAME,
    PI_VERSION,
    PiRuntimeAdapter,
    PiRuntimeError,
    PiRuntimeResolution,
    PiRuntimeUnavailable,
    PiRuntimeVersionError,
)
from .tools import ToolRegistry
from .context import build_pi_prompt, event_owner_key
from .worker import (
    WORKER_DESCRIPTOR_KEY,
    PiWorkerConfig,
    WorkerConfigFactory,
    descriptor_for_worker_config,
    descriptor_from_task,
    validate_resource_paths,
    with_agent_dir,
    worker_config_from_descriptor,
)

__all__ = [
    "ArtifactRecord",
    "AstrBotAdapter",
    "AstrBotAdapterError",
    "InvalidTaskTransition",
    "PiEvent",
    "PiProcessState",
    "PiRpcAdapter",
    "PiRpcError",
    "SnapshotRecord",
    "TaskNotFoundError",
    "TaskRecord",
    "TaskRegistry",
    "TaskRegistryError",
    "TaskStatus",
    "TaskScheduler",
    "PiTaskService",
    "NODE_VERSION",
    "PI_PACKAGE_NAME",
    "PI_VERSION",
    "PiRuntimeAdapter",
    "PiRuntimeError",
    "PiRuntimeResolution",
    "PiRuntimeUnavailable",
    "PiRuntimeVersionError",
    "ToolRegistry",
    "build_pi_prompt",
    "event_owner_key",
    "UnsupportedAstrBotCapability",
    "WakeMainAgent",
    "WORKER_DESCRIPTOR_KEY",
    "PiWorkerConfig",
    "WorkerConfigFactory",
    "descriptor_for_worker_config",
    "descriptor_from_task",
    "validate_resource_paths",
    "with_agent_dir",
    "worker_config_from_descriptor",
]
