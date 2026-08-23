"""Resolve the plugin-owned Node and Pi CLI without modifying Pi itself.

The bridge launches Pi through its public CLI/RPC interface.  This module only
selects an argv prefix for that process; it neither patches the bundled Pi code
nor shells out while resolving a runtime.

The canonical packaged layout is::

    runtime/
      node/
        linux-x64/bin/node
        win-x64/node.exe
      pi/0.84.2/
        node_modules/@earendil-works/pi-coding-agent/dist/cli.js
        node_modules/@earendil-works/pi-coding-agent/package.json

Several flat ``pi-0.84.2`` and ``node-<platform>`` variants are accepted to
keep archive extraction platform-neutral.  When no bundled runtime exists, a
normal ``pi`` executable on ``PATH`` remains a deliberate fallback.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

PI_VERSION = "0.84.2"
NODE_VERSION = "22.19.0"
PI_PACKAGE_NAME = "@earendil-works/pi-coding-agent"

CommandPart: TypeAlias = str | os.PathLike[str]
ExecutableCommand: TypeAlias = CommandPart | Sequence[CommandPart]
RuntimeSource: TypeAlias = Literal["bundled", "configured", "path"]
Which = Callable[..., str | None]

_WINDOWS_PATH = re.compile(r"^(?P<drive>[a-zA-Z]):[\\/]*(?P<tail>.*)$")
_WSL_PATH = re.compile(r"^/mnt/(?P<drive>[a-zA-Z])(?:/(?P<tail>.*))?$")


class PiRuntimeError(RuntimeError):
    """Base error for runtime selection failures."""


class PiRuntimeUnavailable(PiRuntimeError):
    """Raised when neither the bundled runtime nor PATH can launch Pi."""


class PiRuntimeVersionError(PiRuntimeError):
    """Raised when a bundled Pi manifest does not match the fixed version."""


@dataclass(frozen=True, slots=True)
class PiRuntimeResolution:
    """A resolved command prefix and the source used to obtain it."""

    command: tuple[str, ...]
    source: RuntimeSource
    platform_tag: str
    pi_version: str | None
    node_path: str | None = None
    cli_path: str | None = None


class PiRuntimeAdapter:
    """Resolve the fixed Pi 0.84.2 runtime for the current host.

    ``configured_command`` is an explicit override and is preserved as argv;
    it may be a string/path or a sequence such as ``[node, cli_js]``.  Without
    an override the plugin-owned runtime is preferred, followed by a ``pi``
    executable found on PATH.  Command resolution never invokes a shell.
    """

    def __init__(
        self,
        *,
        plugin_root: str | os.PathLike[str] | None = None,
        runtime_root: str | os.PathLike[str] | None = None,
        configured_command: ExecutableCommand | None = None,
        platform_name: str | None = None,
        machine: str | None = None,
        is_wsl: bool | None = None,
        environment: Mapping[str, str] | None = None,
        which: Which = shutil.which,
    ) -> None:
        if plugin_root is None:
            plugin_root = Path(__file__).resolve().parents[1]
        self.plugin_root = Path(plugin_root).expanduser().resolve(strict=False)
        self.runtime_root = (
            Path(runtime_root).expanduser().resolve(strict=False)
            if runtime_root is not None
            else self.plugin_root / "runtime"
        )
        self.configured_command = configured_command
        self.platform_name = (platform_name or platform.system()).lower()
        self.machine = (machine or platform.machine()).lower()
        self.environment = dict(environment or os.environ)
        self.is_wsl = self._detect_wsl() if is_wsl is None else is_wsl
        self._which = which

    @property
    def platform_tag(self) -> str:
        """Return the bundle directory tag appropriate for this host."""

        if self.platform_name.startswith("win"):
            return "win-x64"
        if self.platform_name.startswith("darwin") or self.platform_name.startswith("mac"):
            return "darwin-arm64" if _is_arm64(self.machine) else "darwin-x64"
        # WSL executes Linux ELF binaries, even when the plugin source resides
        # below /mnt/c.  It must therefore choose the Linux bundle.
        return "linux-arm64" if _is_arm64(self.machine) else "linux-x64"

    @property
    def is_windows(self) -> bool:
        return self.platform_name.startswith("win")

    def resolve(self) -> PiRuntimeResolution:
        """Return a complete command prefix or raise a precise runtime error."""

        if self.configured_command is not None:
            return PiRuntimeResolution(
                command=self.normalize_command(self.configured_command),
                source="configured",
                platform_tag=self.platform_tag,
                pi_version=None,
            )

        bundled = self._resolve_bundled()
        if bundled is not None:
            return bundled

        fallback = self._resolve_path_fallback()
        if fallback is not None:
            return PiRuntimeResolution(
                command=(fallback,),
                source="path",
                platform_tag=self.platform_tag,
                pi_version=None,
            )

        raise PiRuntimeUnavailable(
            "Pi runtime not found: package the fixed runtime under "
            f"{self.runtime_root} or install 'pi' on PATH"
        )

    def resolve_command(self) -> tuple[str, ...]:
        """Return only the argv prefix used by :class:`PiRpcAdapter`."""

        return self.resolve().command

    def normalize_command(self, command: ExecutableCommand) -> tuple[str, ...]:
        """Validate a shell-free command prefix and normalize WSL paths."""

        if isinstance(command, (str, os.PathLike)):
            parts = (os.fspath(command),)
        elif isinstance(command, Sequence):
            parts = tuple(os.fspath(part) for part in command)
        else:
            raise TypeError("Pi command must be a string, path, or sequence of strings/paths")
        if not parts or any(not isinstance(part, str) or not part.strip() for part in parts):
            raise ValueError("Pi command cannot be empty")
        return tuple(self.normalize_path(part) for part in parts)

    def normalize_path(self, value: str) -> str:
        """Translate Windows and WSL absolute path notation for the host."""

        if self.is_wsl:
            windows = _WINDOWS_PATH.match(value)
            if windows:
                tail = windows.group("tail").replace("\\", "/").lstrip("/")
                base = f"/mnt/{windows.group('drive').lower()}"
                return f"{base}/{tail}" if tail else base
        if self.is_windows:
            wsl = _WSL_PATH.match(value)
            if wsl:
                tail = (wsl.group("tail") or "").replace("/", "\\")
                base = f"{wsl.group('drive').upper()}:\\"
                return f"{base}{tail}" if tail else base
        return value

    def _resolve_bundled(self) -> PiRuntimeResolution | None:
        node = _first_existing_file(self._node_candidates())
        cli = _first_existing_file(self._cli_candidates())
        if node is None and cli is None:
            return None
        if node is None or cli is None:
            missing = "Node" if node is None else f"Pi {PI_VERSION} CLI"
            raise PiRuntimeUnavailable(
                f"Bundled Pi runtime under {self.runtime_root} is incomplete: missing {missing}"
            )

        self._validate_pi_version(cli)
        return PiRuntimeResolution(
            command=(str(node), str(cli)),
            source="bundled",
            platform_tag=self.platform_tag,
            pi_version=PI_VERSION,
            node_path=str(node),
            cli_path=str(cli),
        )

    def _node_candidates(self) -> tuple[Path, ...]:
        executable = "node.exe" if self.is_windows else "node"
        tag = self.platform_tag
        return (
            self.runtime_root / "node" / tag / ("node.exe" if self.is_windows else "bin/node"),
            self.runtime_root / "node" / NODE_VERSION / tag / ("node.exe" if self.is_windows else "bin/node"),
            self.runtime_root / f"node-{tag}" / executable,
            self.runtime_root / f"node-v{NODE_VERSION}-{tag}" / executable,
            self.runtime_root / f"node-{NODE_VERSION}-{tag}" / executable,
        )

    def _cli_candidates(self) -> tuple[Path, ...]:
        package_cli = Path("node_modules") / PI_PACKAGE_NAME / "dist" / "cli.js"
        roots = (
            self.runtime_root / "pi" / PI_VERSION,
            self.runtime_root / f"pi-{PI_VERSION}",
            self.runtime_root / "pi",
        )
        result: list[Path] = []
        for root in roots:
            result.extend((root / package_cli, root / "dist" / "cli.js", root / "cli.js"))
        return tuple(result)

    def _validate_pi_version(self, cli: Path) -> None:
        package_root = _find_package_root(cli)
        manifest = package_root / "package.json" if package_root else None
        if manifest is None or not manifest.is_file():
            # A trimmed production artifact may retain only dist/. Its enclosing
            # directory remains version-pinned by the expected runtime layout.
            return
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PiRuntimeVersionError(f"Invalid bundled Pi manifest: {manifest}") from exc
        package_name = payload.get("name")
        version = payload.get("version")
        if package_name not in {None, PI_PACKAGE_NAME}:
            raise PiRuntimeVersionError(
                f"Bundled Pi manifest names {package_name!r}, expected {PI_PACKAGE_NAME!r}"
            )
        if version != PI_VERSION:
            raise PiRuntimeVersionError(
                f"Bundled Pi version is {version!r}, expected {PI_VERSION!r}"
            )

    def _resolve_path_fallback(self) -> str | None:
        path = self.environment.get("PATH")
        candidates = ("pi", "pi.exe", "pi.cmd") if self.is_windows else ("pi",)
        for candidate in candidates:
            value = self._call_which(candidate, path)
            if value:
                return self.normalize_path(value)
        return None

    def _call_which(self, candidate: str, path: str | None) -> str | None:
        try:
            return self._which(candidate, path=path)
        except TypeError:
            # Lightweight tests often inject a one-argument callable.
            return self._which(candidate)

    def _detect_wsl(self) -> bool:
        if not self.platform_name.startswith("linux"):
            return False
        if self.environment.get("WSL_DISTRO_NAME") or self.environment.get("WSL_INTEROP"):
            return True
        try:
            return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
        except OSError:
            return False


def _first_existing_file(candidates: Sequence[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve(strict=False)
    return None


def _find_package_root(cli: Path) -> Path | None:
    """Find the nearest package root above ``dist/cli.js``."""

    for parent in (cli.parent, *cli.parents):
        if parent.name == "dist":
            return parent.parent
    return None


def _is_arm64(machine: str) -> bool:
    return machine in {"aarch64", "arm64"}


__all__ = [
    "CommandPart",
    "ExecutableCommand",
    "NODE_VERSION",
    "PI_PACKAGE_NAME",
    "PI_VERSION",
    "PiRuntimeAdapter",
    "PiRuntimeError",
    "PiRuntimeResolution",
    "PiRuntimeUnavailable",
    "PiRuntimeVersionError",
]
