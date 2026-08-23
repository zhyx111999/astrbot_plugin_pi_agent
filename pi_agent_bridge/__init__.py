"""Independent Pi RPC bridge used by the task-oriented plugin runtime.

The legacy :mod:`pi_connector` package remains untouched.  This package is a
small adapter around Pi's public ``--mode rpc`` JSONL interface and deliberately
does not own task scheduling or AstrBot message delivery.
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
from .wakeup import WakeupAdapter
from .context import TaskContext, capture_task_context
from .astrbot_context_adapter import AstrBotContextAdapter, CapturedAstrBotContext
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
    "WakeupAdapter",
    "TaskContext",
    "capture_task_context",
    "AstrBotContextAdapter",
    "CapturedAstrBotContext",
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
