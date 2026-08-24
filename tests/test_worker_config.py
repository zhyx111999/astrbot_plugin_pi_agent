"""Tests for transient, secret-free Pi worker configuration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

# isort: off
import _helpers  # noqa: F401
from pi_agent_bridge.worker import (  # noqa: E402
    WORKER_DESCRIPTOR_KEY,
    PiWorkerConfig,
    descriptor_for_worker_config,
    descriptor_from_task,
    validate_resource_paths,
    with_agent_dir,
    worker_config_from_descriptor,
)
# isort: on


def test_descriptor_omits_environment_values() -> None:
    config = PiWorkerConfig(
        provider="  gateway  ",
        model=" gpt-test ",
        environment={"PI_API_KEY": "sk-secret", "PI_ORG": "tenant"},
        skill_paths=("/skills/research",),
        extension_paths=("/extensions/tools",),
        agent_dir="/tmp/pi-agent",
    )

    descriptor = descriptor_for_worker_config(config)

    assert descriptor == {
        "provider": "gateway",
        "model": "gpt-test",
        "agent_dir": "/tmp/pi-agent",
        "skill_paths": ["/skills/research"],
        "extension_paths": ["/extensions/tools"],
        "environment_keys": ["PI_API_KEY", "PI_ORG"],
        "thinking_level": "max",
    }
    serialized = json.dumps(descriptor)
    assert "environment" not in descriptor
    assert "sk-secret" not in serialized
    assert "tenant" not in serialized


def test_descriptor_is_detached_from_mutable_worker_values() -> None:
    environment = {"PI_API_KEY": "sk-secret"}
    config = PiWorkerConfig(environment=environment)

    descriptor = descriptor_for_worker_config(config)
    environment["PI_NEW_SECRET"] = "another-secret"

    assert descriptor["environment_keys"] == ["PI_API_KEY"]


def test_with_agent_dir_resolves_only_unbound_configs(tmp_path: Path) -> None:
    target = tmp_path / "agents" / "task-1"
    config = PiWorkerConfig(provider="gateway", environment={"KEY": "value"})

    bound = with_agent_dir(config, target)
    assert bound.agent_dir == str(target.resolve())
    assert bound.environment == {"KEY": "value"}
    assert bound.provider == "gateway"

    explicit = PiWorkerConfig(agent_dir="/already/bound")
    assert with_agent_dir(explicit, target) is explicit


def test_descriptor_from_task_reads_only_mapping_context() -> None:
    descriptor = {"provider": "gateway", "environment_keys": []}
    task = SimpleNamespace(context={WORKER_DESCRIPTOR_KEY: descriptor})

    assert descriptor_from_task(task) is descriptor
    assert descriptor_from_task(SimpleNamespace(context=None)) is None
    assert descriptor_from_task(SimpleNamespace(context={WORKER_DESCRIPTOR_KEY: "bad"})) is None


def test_resume_requires_host_to_rehydrate_credentials() -> None:
    descriptor = {
        "provider": "gateway",
        "model": "gpt-test",
        "agent_dir": "/tmp/task-agent",
        "environment_keys": ["PI_API_KEY"],
    }

    with pytest.raises(ValueError, match="rehydrated"):
        worker_config_from_descriptor(descriptor)


def test_keyless_descriptor_can_be_rebuilt_without_secrets() -> None:
    config = worker_config_from_descriptor(
        {
            "provider": "local",
            "model": "model.gguf",
            "agent_dir": "/tmp/task-agent",
            "skill_paths": ["/skills/a"],
            "extension_paths": ["/extensions/b"],
            "environment_keys": [],
        }
    )

    assert config.provider == "local"
    assert config.model == "model.gguf"
    assert config.agent_dir == "/tmp/task-agent"
    assert config.skill_paths == ("/skills/a",)
    assert config.extension_paths == ("/extensions/b",)
    assert config.environment == {}


def test_validate_resource_paths_normalizes_and_deduplicates(tmp_path: Path) -> None:
    resource = tmp_path / "skill"
    resource.mkdir()

    result = validate_resource_paths(
        [resource, str(resource)],
        label="skill_paths",
    )

    assert result == (str(resource.resolve()),)


def test_validate_resource_paths_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        validate_resource_paths(
            [tmp_path / "missing"],
            label="extension_paths",
        )
