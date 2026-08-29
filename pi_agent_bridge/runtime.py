"""Resolve the plugin-owned Node and Pi CLI without modifying Pi itself.

The bridge launches Pi through its public CLI/RPC interface.  This module only
selects an argv prefix for that process; it neither patches the bundled Pi code
nor shells out while resolving a runtime.

The canonical extracted layout is::

    runtime/
      vendor/pi-runtime-linux-x64.tar.xz
      node/linux-x64/bin/node
      pi/0.84.2/node_modules/@earendil-works/pi-coding-agent/dist/cli.js

The vendor archive is the packaged payload. Plugin initialization unpacks it
into the extracted layout automatically; resolve() repeats that check before
launching a worker. If the archive is missing, the plugin downloads it from
the GitHub Release asset and, on failure, retries the same URL through the
AstrBot GitHub mirror prefixes. Several flat ``pi-0.84.2`` and
``node-<platform>`` variants remain accepted. When no bundled archive exists,
a ``pi`` executable on PATH is the fallback.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import tarfile
import threading
import urllib.error
import urllib.request
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
_EXTRACT_LOCK = threading.Lock()
VENDOR_ARCHIVE_NAME = "pi-runtime-{platform_tag}.tar.xz"
BUNDLED_RUNTIME_DOWNLOAD = (
    "https://github.com/zhyx111999/astrbot_plugin_pi_agent/releases/"
    "latest/download/pi-runtime-{platform_tag}.tar.xz"
)
GITHUB_RELEASE_MIRRORS = (
    "https://edgeone.gh-proxy.com",
    "https://hk.gh-proxy.com",
    "https://gh-proxy.com",
    "https://gh.dpik.top",
)
DOWNLOAD_TIMEOUT_SECONDS = 30
logger = logging.getLogger(__name__)


def release_archive_urls(platform_tag: str) -> tuple[str, ...]:
    """Official GitHub Release URL first, then AstrBot-style ghproxy prefixes."""

    official = BUNDLED_RUNTIME_DOWNLOAD.format(platform_tag=platform_tag)
    return (
        official,
        *(f"{mirror.rstrip('/')}/{official}" for mirror in GITHUB_RELEASE_MIRRORS),
    )


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

        self._ensure_vendor_extracted()
        bundled = self._resolve_bundled()
        if bundled is not None:
            return bundled

        fallback = self._resolve_path_fallback()
        if fallback is not None:
            return PiRuntimeResolution(
                command=fallback,
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

    def _vendor_archive(self) -> Path:
        return self.runtime_root / "vendor" / VENDOR_ARCHIVE_NAME.format(
            platform_tag=self.platform_tag
        )

    def bundled_runtime_ready(self) -> bool:
        """Return whether the extracted Node binary and Pi CLI are both present."""

        return (
            _first_existing_file(self._node_candidates()) is not None
            and _first_existing_file(self._cli_candidates()) is not None
        )

    def install_bundled_runtime(self, *, allow_download: bool = True) -> bool:
        """Unpack the vendor archive after plugin install, then drop it.

        On WSL/Linux this is the install path: find or download the compressed
        linux-x64 archive, extract Node/Pi, mark the Node binary executable,
        then delete the archive so disk does not keep both copies.
        """

        with _EXTRACT_LOCK:
            if self.configured_command is not None:
                return False
            if not self.bundled_runtime_ready():
                archive = self._vendor_archive()
                if not archive.is_file() and allow_download:
                    archive = self._fetch_vendor_archive() or archive
                if archive.is_file():
                    self._extract_vendor_archive(archive)
            self._chmod_extracted_node()
            ready = self.bundled_runtime_ready()
            if ready:
                self._discard_vendor_archives()
            return ready

    def _ensure_vendor_extracted(self) -> None:
        """Materialize a local vendor archive before launching a worker."""

        if self.configured_command is not None or self.bundled_runtime_ready():
            return
        archive = self._vendor_archive()
        if not archive.is_file():
            return
        with _EXTRACT_LOCK:
            if self.bundled_runtime_ready():
                return
            self._extract_vendor_archive(archive)
            self._chmod_extracted_node()

    def _fetch_vendor_archive(self) -> Path | None:
        if not self.platform_tag.startswith("linux"):
            return None
        dest = self._vendor_archive()
        dest.parent.mkdir(parents=True, exist_ok=True)
        for url in release_archive_urls(self.platform_tag):
            if self._download_url_to(url, dest):
                logger.info("Downloaded bundled Pi runtime from %s", url)
                return dest
            logger.warning("Failed to download bundled Pi runtime from %s", url)
        return None

    def _download_url_to(self, url: str, dest: Path) -> bool:
        partial = dest.with_name(dest.name + ".partial")
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "astrbot-plugin-pi-agent"}
            )
            with urllib.request.urlopen(
                request, timeout=DOWNLOAD_TIMEOUT_SECONDS
            ) as response, partial.open("wb") as out:
                shutil.copyfileobj(response, out)
            if partial.stat().st_size < 32:
                raise PiRuntimeUnavailable(f"Downloaded runtime archive is empty: {url}")
            partial.replace(dest)
            return True
        except (OSError, urllib.error.URLError, PiRuntimeUnavailable):
            if partial.exists():
                partial.unlink(missing_ok=True)
            return False

    def _extract_vendor_archive(self, archive: Path) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:xz") as bundle:
            try:
                bundle.extractall(self.runtime_root, filter="data")
            except TypeError:
                bundle.extractall(self.runtime_root)

    def _chmod_extracted_node(self) -> None:
        if self.is_windows:
            return
        for candidate in self._node_candidates():
            if candidate.is_file():
                candidate.chmod(candidate.stat().st_mode | 0o111)

    def _discard_vendor_archives(self) -> None:
        vendor = self.runtime_root / "vendor"
        if not vendor.is_dir():
            return
        for path in vendor.iterdir():
            if path.is_file() and path.suffix.lower() in {".xz", ".tgz", ".gz"}:
                try:
                    path.unlink()
                except OSError:
                    continue

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

    def _resolve_path_fallback(self) -> tuple[str, ...] | None:
        path = self.environment.get("PATH")
        candidates = ("pi", "pi.exe", "pi.cmd") if self.is_windows else ("pi",)
        for candidate in candidates:
            value = self._call_which(candidate, path)
            if value:
                return self._command_for_pi_path(self.normalize_path(value))
        for candidate in self._user_install_candidates():
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return self._command_for_pi_path(candidate)
        return None

    def _command_for_pi_path(self, pi_path: str | Path) -> tuple[str, ...]:
        """Run Node-based Pi scripts without relying on the service PATH."""

        raw_path = os.fspath(pi_path)
        path = Path(raw_path)
        if self.is_windows or path.suffix.lower() not in {"", ".js"} or not path.is_file():
            return (raw_path,)
        try:
            resolved = path.resolve(strict=True)
            first_line = resolved.open(
                "r", encoding="utf-8", errors="replace"
            ).readline()
        except (OSError, UnicodeDecodeError):
            return (raw_path,)
        if "node" not in first_line.lower():
            return (raw_path,)
        node_candidates = (
            path.parent / "node",
            resolved.parent / "node",
            resolved.parent.parent / "bin" / "node",
        )
        for node in node_candidates:
            if node.is_file() and os.access(node, os.X_OK):
                return (str(node), str(resolved))
        return (raw_path,)

    def _user_install_candidates(self) -> tuple[Path, ...]:
        """Find user-level Node installs when a service omits shell PATH setup."""

        if self.is_windows:
            return ()
        home = Path(self.environment.get("HOME") or Path.home())
        candidates = [home / ".local" / "bin" / "pi"]
        nvm_root = home / ".nvm" / "versions" / "node"
        if nvm_root.is_dir():
            candidates.extend(
                version / "bin" / "pi"
                for version in sorted(nvm_root.iterdir(), reverse=True)
                if version.is_dir()
            )
        return tuple(candidates)

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
    "DOWNLOAD_TIMEOUT_SECONDS",
    "GITHUB_RELEASE_MIRRORS",
    "NODE_VERSION",
    "PI_PACKAGE_NAME",
    "PI_VERSION",
    "VENDOR_ARCHIVE_NAME",
    "PiRuntimeAdapter",
    "PiRuntimeError",
    "PiRuntimeResolution",
    "PiRuntimeUnavailable",
    "PiRuntimeVersionError",
    "release_archive_urls",
]
