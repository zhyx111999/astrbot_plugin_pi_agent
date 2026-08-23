"""Artifact discovery and serialization for Pi task workspaces."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any


_KIND_BY_MIME_PREFIX = {
    "audio/": "audio",
    "image/": "image",
    "video/": "video",
}


def classify_artifact(path: Path) -> tuple[str, str | None]:
    """Return a stable artifact kind and the platform MIME guess."""

    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type:
        for prefix, kind in _KIND_BY_MIME_PREFIX.items():
            if mime_type.startswith(prefix):
                return kind, mime_type
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown", ".txt", ".rst"}:
        return "markdown" if suffix in {".md", ".markdown"} else "text", mime_type
    if suffix in {".json", ".jsonl", ".yaml", ".yml", ".toml"}:
        return "json" if suffix in {".json", ".jsonl"} else "data", mime_type
    return "file", mime_type


def artifact_metadata(path: Path, *, hash_limit_bytes: int = 32 * 1024 * 1024) -> dict[str, Any]:
    """Collect bounded metadata without reading unbounded artifact contents."""

    stat = path.stat()
    digest = None
    if stat.st_size <= hash_limit_bytes:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    kind, mime_type = classify_artifact(path)
    return {
        "kind": kind,
        "path": str(path),
        "mime_type": mime_type,
        "size_bytes": stat.st_size,
        "sha256": digest,
        "metadata": {"filename": path.name, "modified_at": stat.st_mtime},
    }


def discover_workspace_artifacts(
    workspace: str | Path,
    *,
    max_files: int = 100,
    max_file_bytes: int = 512 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Discover regular files in a task workspace with bounded traversal."""

    root = Path(workspace)
    if not root.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if len(results) >= max_files:
            break
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
            results.append(artifact_metadata(path))
        except OSError:
            continue
    return results

