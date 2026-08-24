"""Focused tests for plugin-owned Pi runtime command resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_agent_bridge.rpc import PiRpcAdapter
from pi_agent_bridge.runtime import (
    PI_PACKAGE_NAME,
    PI_VERSION,
    PiRuntimeAdapter,
    PiRuntimeUnavailable,
    PiRuntimeVersionError,
)


def _write_bundled_runtime(root: Path, *, version: str = PI_VERSION) -> tuple[Path, Path]:
    node = root / "node" / "linux-x64" / "bin" / "node"
    cli = (
        root
        / "pi"
        / PI_VERSION
        / "node_modules"
        / PI_PACKAGE_NAME
        / "dist"
        / "cli.js"
    )
    node.parent.mkdir(parents=True)
    cli.parent.mkdir(parents=True)
    node.write_text("node", encoding="utf-8")
    cli.write_text("cli", encoding="utf-8")
    (cli.parent.parent / "package.json").write_text(
        json.dumps({"name": PI_PACKAGE_NAME, "version": version}), encoding="utf-8"
    )
    return node, cli


def test_bundled_runtime_prefers_fixed_node_and_pi_cli(tmp_path: Path):
    node, cli = _write_bundled_runtime(tmp_path / "runtime")
    adapter = PiRuntimeAdapter(
        runtime_root=tmp_path / "runtime",
        platform_name="Linux",
        machine="x86_64",
        is_wsl=False,
        which=lambda *_args, **_kwargs: "/usr/bin/pi",
    )

    resolution = adapter.resolve()

    assert resolution.source == "bundled"
    assert resolution.pi_version == PI_VERSION
    assert resolution.command == (str(node.resolve()), str(cli.resolve()))


def test_path_fallback_when_no_bundled_runtime(tmp_path: Path):
    adapter = PiRuntimeAdapter(
        runtime_root=tmp_path / "missing-runtime",
        platform_name="Linux",
        machine="x86_64",
        is_wsl=False,
        which=lambda candidate, **_kwargs: "/usr/local/bin/pi" if candidate == "pi" else None,
    )

    resolution = adapter.resolve()

    assert resolution.source == "path"
    assert resolution.command == ("/usr/local/bin/pi",)


def test_missing_node_or_cli_is_an_explicit_error(tmp_path: Path):
    runtime = tmp_path / "runtime"
    node = runtime / "node" / "linux-x64" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("node", encoding="utf-8")
    adapter = PiRuntimeAdapter(
        runtime_root=runtime,
        platform_name="Linux",
        machine="x86_64",
        is_wsl=False,
        which=lambda *_args, **_kwargs: None,
    )

    with pytest.raises(PiRuntimeUnavailable, match="incomplete"):
        adapter.resolve()


def test_manifest_version_must_match_fixed_pi_version(tmp_path: Path):
    _write_bundled_runtime(tmp_path / "runtime", version="0.84.1")
    adapter = PiRuntimeAdapter(
        runtime_root=tmp_path / "runtime",
        platform_name="Linux",
        machine="x86_64",
        is_wsl=False,
        which=lambda *_args, **_kwargs: None,
    )

    with pytest.raises(PiRuntimeVersionError, match="expected '0.84.2'"):
        adapter.resolve()


def test_windows_and_wsl_path_notation_is_normalized_without_shell(tmp_path: Path):
    wsl = PiRuntimeAdapter(
        runtime_root=tmp_path,
        configured_command=[r"C:\Pi Runtime\node.exe", r"C:\Pi Runtime\cli.js"],
        platform_name="Linux",
        machine="x86_64",
        is_wsl=True,
    )
    windows = PiRuntimeAdapter(
        runtime_root=tmp_path,
        configured_command=["/mnt/c/Pi Runtime/node.exe", "/mnt/c/Pi Runtime/cli.js"],
        platform_name="Windows",
        machine="AMD64",
        is_wsl=False,
    )

    assert wsl.resolve_command() == (
        "/mnt/c/Pi Runtime/node.exe",
        "/mnt/c/Pi Runtime/cli.js",
    )
    assert windows.resolve_command() == (
        r"C:\Pi Runtime\node.exe",
        r"C:\Pi Runtime\cli.js",
    )


def test_rpc_adapter_keeps_command_prefix_as_individual_argv_tokens():
    adapter = PiRpcAdapter(executable=["/opt/node with spaces/bin/node", "/opt/pi/cli.js"])

    assert adapter.command_line()[:5] == [
        "/opt/node with spaces/bin/node",
        "/opt/pi/cli.js",
        "--mode",
        "rpc",
    ]
