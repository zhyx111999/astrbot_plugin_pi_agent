"""Independent Pi Agent runtime adapters used by the task-oriented plugin.

This package adapts Pi's public ``--mode rpc`` JSONL interface without
modifying Pi or delivering messages into AstrBot chats.
"""

from .rpc import (
    PiEvent,
    PiProcessState,
    PiRpcAdapter,
    PiRpcError,
)
from .models import TaskRecord, TaskStatus
from .registry import InvalidTaskTransition, TaskNotFoundError, TaskRegistry, TaskRegistryError
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
from .context import build_pi_prompt, event_owner_key, event_session_origin, owner_user_id
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
    "InvalidTaskTransition",
    "PiEvent",
    "PiProcessState",
    "PiRpcAdapter",
    "PiRpcError",
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
    "build_pi_prompt",
    "event_owner_key",
    "event_session_origin",
    "owner_user_id",
    "WORKER_DESCRIPTOR_KEY",
    "PiWorkerConfig",
    "WorkerConfigFactory",
    "descriptor_for_worker_config",
    "descriptor_from_task",
    "validate_resource_paths",
    "with_agent_dir",
    "worker_config_from_descriptor",
]
