"""Focused tests for plugin-owned Pi runtime command resolution."""

from __future__ import annotations

import io
import json
import os
import tarfile
import urllib.error
from pathlib import Path

import pytest

from pi_agent_bridge.rpc import PiRpcAdapter
from pi_agent_bridge.runtime import (
    GITHUB_RELEASE_MIRRORS,
    PI_PACKAGE_NAME,
    PI_VERSION,
    PiRuntimeAdapter,
    PiRuntimeUnavailable,
    PiRuntimeVersionError,
    release_archive_urls,
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


def _vendor_archive_bytes() -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:xz") as bundle:
        node_bytes = b"#!/bin/node\n"
        node_info = tarfile.TarInfo("node/linux-x64/bin/node")
        node_info.mode = 0o644
        node_info.size = len(node_bytes)
        bundle.addfile(node_info, io.BytesIO(node_bytes))
        cli_bytes = b"cli"
        cli_info = tarfile.TarInfo(
            "pi/0.84.2/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
        )
        cli_info.size = len(cli_bytes)
        bundle.addfile(cli_info, io.BytesIO(cli_bytes))
        manifest = json.dumps(
            {"name": PI_PACKAGE_NAME, "version": PI_VERSION}
        ).encode()
        manifest_info = tarfile.TarInfo(
            "pi/0.84.2/node_modules/@earendil-works/pi-coding-agent/package.json"
        )
        manifest_info.size = len(manifest)
        bundle.addfile(manifest_info, io.BytesIO(manifest))
    return payload.getvalue()


class _DownloadResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return io.BytesIO(self._payload)

    def __exit__(self, *_args):
        return False


def test_vendor_archive_is_extracted_into_canonical_layout(tmp_path: Path):
    runtime = tmp_path / "runtime"
    vendor = runtime / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "pi-runtime-linux-x64.tar.xz").write_bytes(_vendor_archive_bytes())

    adapter = PiRuntimeAdapter(
        runtime_root=runtime,
        platform_name="Linux",
        machine="x86_64",
        is_wsl=False,
        which=lambda *_args, **_kwargs: None,
    )
    assert adapter.install_bundled_runtime() is True
    resolution = adapter.resolve()

    assert resolution.source == "bundled"
    assert resolution.pi_version == PI_VERSION
    assert Path(resolution.command[0]).name == "node"
    assert Path(resolution.command[1]).name == "cli.js"
    assert not (vendor / "pi-runtime-linux-x64.tar.xz").exists()


def test_release_archive_urls_try_github_before_mirrors():
    urls = release_archive_urls("linux-x64")
    official = (
        "https://github.com/zhyx111999/astrbot_plugin_pi_agent/releases/"
        "latest/download/pi-runtime-linux-x64.tar.xz"
    )
    assert urls[0] == official
    assert urls[1:] == tuple(
        f"{mirror}/{official}" for mirror in GITHUB_RELEASE_MIRRORS
    )


def test_install_downloads_vendor_archive_when_missing(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    archive_bytes = _vendor_archive_bytes()
    requested: list[str] = []

    def fake_urlopen(request, timeout=None):
        requested.append(request.full_url)
        return _DownloadResponse(archive_bytes)

    monkeypatch.setattr("pi_agent_bridge.runtime.urllib.request.urlopen", fake_urlopen)
    adapter = PiRuntimeAdapter(
        runtime_root=runtime,
        platform_name="Linux",
        machine="x86_64",
        is_wsl=True,
        which=lambda *_args, **_kwargs: None,
    )

    assert adapter.install_bundled_runtime(allow_download=True) is True
    node = runtime / "node" / "linux-x64" / "bin" / "node"
    assert node.is_file()
    if os.name != "nt":
        assert node.stat().st_mode & 0o111
    assert not (runtime / "vendor" / "pi-runtime-linux-x64.tar.xz").exists()
    assert requested == [release_archive_urls("linux-x64")[0]]


def test_install_falls_back_to_github_mirror_when_direct_download_fails(
    tmp_path: Path, monkeypatch
):
    runtime = tmp_path / "runtime"
    archive_bytes = _vendor_archive_bytes()
    requested: list[str] = []
    official, first_mirror, *_rest = release_archive_urls("linux-x64")

    def fake_urlopen(request, timeout=None):
        requested.append(request.full_url)
        if request.full_url == official:
            raise urllib.error.URLError("github blocked")
        if request.full_url == first_mirror:
            return _DownloadResponse(archive_bytes)
        raise urllib.error.URLError("unexpected mirror")

    monkeypatch.setattr("pi_agent_bridge.runtime.urllib.request.urlopen", fake_urlopen)
    adapter = PiRuntimeAdapter(
        runtime_root=runtime,
        platform_name="Linux",
        machine="x86_64",
        is_wsl=True,
        which=lambda *_args, **_kwargs: None,
    )

    assert adapter.install_bundled_runtime(allow_download=True) is True
    assert requested == [official, first_mirror]
    assert (runtime / "node" / "linux-x64" / "bin" / "node").is_file()
    assert not (runtime / "vendor" / "pi-runtime-linux-x64.tar.xz").exists()


def test_install_returns_false_when_direct_and_mirrors_fail(tmp_path: Path, monkeypatch):
    requested: list[str] = []

    def fake_urlopen(request, timeout=None):
        requested.append(request.full_url)
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr("pi_agent_bridge.runtime.urllib.request.urlopen", fake_urlopen)
    adapter = PiRuntimeAdapter(
        runtime_root=tmp_path / "runtime",
        platform_name="Linux",
        machine="x86_64",
        is_wsl=True,
        which=lambda *_args, **_kwargs: None,
    )

    assert adapter.install_bundled_runtime(allow_download=True) is False
    assert requested == list(release_archive_urls("linux-x64"))


def test_install_without_archive_or_download_returns_false(tmp_path: Path):
    adapter = PiRuntimeAdapter(
        runtime_root=tmp_path / "runtime",
        platform_name="Linux",
        machine="x86_64",
        is_wsl=True,
        which=lambda *_args, **_kwargs: None,
    )
    assert adapter.install_bundled_runtime(allow_download=False) is False


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
