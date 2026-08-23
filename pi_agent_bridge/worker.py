"""Transient per-worker launch configuration.

The registry deliberately does not persist this object.  In particular,
``environment`` may contain provider credentials and must only live for the
short period in which the Pi child process is launched.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from os import fspath
from pathlib import Path
from typing import Any, Callable


WORKER_DESCRIPTOR_KEY = "_pi_worker_descriptor"


@dataclass(frozen=True, slots=True)
class PiWorkerConfig:
    """Values applied to one isolated Pi RPC worker."""

    provider: str | None = None
    model: str | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    skill_paths: tuple[str, ...] = ()
    extension_paths: tuple[str, ...] = ()
    agent_dir: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _optional_text(self.provider))
        object.__setattr__(self, "model", _optional_text(self.model))
        object.__setattr__(self, "environment", _environment(self.environment))
        object.__setattr__(self, "skill_paths", _paths(self.skill_paths))
        object.__setattr__(self, "extension_paths", _paths(self.extension_paths))
        object.__setattr__(self, "agent_dir", _optional_text(self.agent_dir))


WorkerConfigFactory = Callable[
    [Any], PiWorkerConfig | Awaitable[PiWorkerConfig]
]


def with_agent_dir(config: PiWorkerConfig, agent_dir: str | Path) -> PiWorkerConfig:
    """Return a worker config bound to one task-owned Pi agent directory."""

    if config.agent_dir:
        return config
    return replace(config, agent_dir=str(Path(agent_dir).expanduser().resolve(strict=False)))


def descriptor_for_worker_config(config: PiWorkerConfig) -> dict[str, Any]:
    """Return only non-secret fields suitable for the durable task context.

    Environment values are intentionally omitted.  ``environment_keys`` is
    metadata only, allowing recovery to detect that a host-side resolver must
    rehydrate credentials before starting a new process.
    """

    return {
        "provider": config.provider,
        "model": config.model,
        "agent_dir": config.agent_dir,
        "skill_paths": list(config.skill_paths),
        "extension_paths": list(config.extension_paths),
        "environment_keys": sorted(config.environment),
    }


def descriptor_from_task(task: Any) -> Mapping[str, Any] | None:
    """Read a non-secret worker descriptor from a durable task record."""

    context = getattr(task, "context", None)
    if not isinstance(context, Mapping):
        return None
    descriptor = context.get(WORKER_DESCRIPTOR_KEY)
    return descriptor if isinstance(descriptor, Mapping) else None


def worker_config_from_descriptor(
    descriptor: Mapping[str, Any],
    *,
    require_environment: bool = True,
) -> PiWorkerConfig:
    """Rebuild a secret-free config from a persisted descriptor.

    A descriptor that records environment variable names but no values cannot
    safely launch a provider requiring credentials.  Callers may set
    ``require_environment=False`` for intentionally keyless local providers.
    """

    if not isinstance(descriptor, Mapping):
        raise TypeError("worker descriptor must be a mapping")
    environment_keys = descriptor.get("environment_keys") or ()
    if require_environment and environment_keys:
        raise ValueError(
            "worker provider credentials must be rehydrated by the host before resume"
        )
    return PiWorkerConfig(
        provider=descriptor.get("provider"),
        model=descriptor.get("model"),
        agent_dir=descriptor.get("agent_dir"),
        skill_paths=tuple(descriptor.get("skill_paths") or ()),
        extension_paths=tuple(descriptor.get("extension_paths") or ()),
    )


def validate_resource_paths(
    value: Any,
    *,
    label: str,
    require_exists: bool = True,
) -> tuple[str, ...]:
    """Normalize configured Pi resource paths without invoking a shell."""

    if value is None or value == "":
        return ()
    if isinstance(value, (str, bytes)):
        candidates: Sequence[Any] = (value,)
    elif isinstance(value, Sequence):
        candidates = value
    else:
        raise ValueError(f"{label} must be a list of paths")

    result: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, (str, Path)) or not fspath(item).strip():
            raise ValueError(f"{label} contains an empty path")
        path = Path(fspath(item)).expanduser().resolve(strict=False)
        if require_exists and not path.exists():
            raise ValueError(f"{label} path does not exist: {path}")
        normalized = str(path)
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _environment(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("worker environment must be a mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        name = str(key).strip()
        if not name:
            raise ValueError("worker environment contains an empty variable name")
        result[name] = str(item)
    return result


def _paths(value: Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(str(item) for item in value)


__all__ = [
    "PiWorkerConfig",
    "WORKER_DESCRIPTOR_KEY",
    "WorkerConfigFactory",
    "descriptor_for_worker_config",
    "descriptor_from_task",
    "validate_resource_paths",
    "with_agent_dir",
    "worker_config_from_descriptor",
]
