"""Capture a task-local AstrBot context through public plugin APIs.

The adapter is deliberately duck-typed.  AstrBot is an optional host for the
bridge's unit tests, and its private main-agent implementation is not a stable
plugin contract.  We therefore read the persisted conversation and resolve the
selected persona through the public ``Context`` managers, while treating media
components as capability probes with bounded, best-effort conversion.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import mimetypes
import os
import shutil
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .context import TaskContext, event_owner_key, snapshot_event_context
from .security import sanitize_value


@dataclass(frozen=True, slots=True)
class CapturedAstrBotContext:
    """Serializable context captured at the moment a task is delegated."""

    task_context: TaskContext
    persona_id: str | None = None
    conversation_id: str | None = None
    media: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def owner_key(self) -> str:
        return self.task_context.owner_key

    @property
    def persona(self) -> str | None:
        return self.task_context.persona

    @property
    def media_references(self) -> tuple[str, ...]:
        return self.task_context.media_references

    def as_dict(self) -> dict[str, Any]:
        result = self.task_context.as_dict()
        result["persona_id"] = self.persona_id
        result["conversation_id"] = self.conversation_id
        result["media"] = [dict(item) for item in self.media]
        result["capture_warnings"] = list(self.warnings)
        return result


class AstrBotContextAdapter:
    """Read current AstrBot context and make media task-owned.

    ``capture`` is intentionally independent of Pi scheduling.  It never calls
    an LLM and never accesses AstrBot's private database fields.  A conversion
    failure is represented in ``warnings`` and the original safe reference is
    retained so the worker can still decide whether it can use the media.
    """

    def __init__(self, context: Any, *, media_timeout_seconds: float = 10.0) -> None:
        self.context = context
        self.media_timeout_seconds = max(float(media_timeout_seconds), 0.1)

    async def capture(
        self,
        event: Any,
        *,
        workspace: str | os.PathLike[str] | None = None,
        inherit_persona: bool = True,
    ) -> CapturedAstrBotContext:
        """Capture persisted history, selected persona, and current media."""

        owner_key = event_owner_key(event)
        warnings: list[str] = []
        conversation: dict[str, Any] = _json_safe(snapshot_event_context(event))
        persona_prompt: str | None = None
        persona_id: str | None = None
        conversation_id: str | None = None

        conversation_data, conversation_id, conversation_warnings = await self._capture_conversation(
            event,
        )
        conversation.update(conversation_data)
        warnings.extend(conversation_warnings)

        if inherit_persona:
            persona_prompt, persona_id, persona_warnings = await self._resolve_persona(
                event,
                conversation.get("persona_id"),
            )
            warnings.extend(persona_warnings)
        else:
            conversation["persona_inherited"] = False

        media, media_references, media_warnings = await self._capture_media(
            event,
            workspace=workspace,
        )
        warnings.extend(media_warnings)
        conversation["media"] = [dict(item) for item in media]
        conversation["media_references"] = list(media_references)
        conversation["capture_warnings"] = list(warnings)

        task_context = TaskContext(
            owner_key=owner_key,
            session_origin=owner_key,
            workspace=str(workspace) if workspace is not None else None,
            persona=persona_prompt,
            conversation=conversation,
            media_references=tuple(media_references),
            warnings=tuple(warnings),
        )
        return CapturedAstrBotContext(
            task_context=task_context,
            persona_id=persona_id,
            conversation_id=conversation_id,
            media=tuple(media),
            warnings=tuple(warnings),
        )

    async def _capture_conversation(
        self,
        event: Any,
    ) -> tuple[dict[str, Any], str | None, list[str]]:
        warnings: list[str] = []
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return {"history": [], "history_available": False}, None, [
                "conversation_manager unavailable; history snapshot is empty",
            ]

        umo = event_owner_key(event)
        current_id = await self._call_public(
            manager,
            "get_curr_conversation_id",
            umo,
            default=None,
        )
        if not isinstance(current_id, str) or not current_id:
            return {
                "conversation_id": None,
                "history": [],
                "history_available": True,
            }, None, warnings

        conversation = await self._call_public(
            manager,
            "get_conversation",
            umo,
            current_id,
            default=None,
        )
        if conversation is None:
            warnings.append("current conversation could not be loaded; history snapshot is empty")
            return {
                "conversation_id": current_id,
                "history": [],
                "history_available": False,
            }, current_id, warnings

        raw_history = getattr(conversation, "history", "[]")
        history = _decode_history(raw_history)
        if history is None:
            warnings.append("conversation history was not valid JSON; history snapshot is empty")
            history = []
        # Conversation history is persisted JSON, but older hosts/test doubles
        # may hand us mutable objects.  Detach and redact it before the task is
        # written to SQLite so later host mutations cannot affect the worker.
        safe_history = _json_safe(history)
        if not isinstance(safe_history, list):
            warnings.append("conversation history could not be serialized safely")
            safe_history = []
        conversation_persona_id = getattr(conversation, "persona_id", None)
        return {
            "conversation_id": current_id,
            "history": safe_history,
            "history_available": True,
            "persona_id": _string_or_none(conversation_persona_id),
        }, current_id, warnings

    async def _resolve_persona(
        self,
        event: Any,
        conversation_persona_id: Any,
    ) -> tuple[str | None, str | None, list[str]]:
        warnings: list[str] = []
        manager = getattr(self.context, "persona_manager", None)
        resolver = getattr(manager, "resolve_selected_persona", None)
        if not callable(resolver):
            return None, None, [
                "persona_manager.resolve_selected_persona unavailable; persona not inherited",
            ]

        umo = event_owner_key(event)
        provider_settings: Mapping[str, Any] = {}
        getter = getattr(self.context, "get_config", None)
        if callable(getter):
            try:
                try:
                    config = getter(umo=umo)
                except TypeError:
                    config = getter(umo)
                config = await _resolve(config)
            except Exception:  # noqa: BLE001
                config = None
            if isinstance(config, Mapping):
                settings = config.get("provider_settings", {})
                if isinstance(settings, Mapping):
                    provider_settings = settings

        platform_name = _call(event, "get_platform_name", "unknown")
        try:
            resolved = resolver(
                umo=umo,
                conversation_persona_id=_string_or_none(conversation_persona_id),
                platform_name=str(platform_name),
                provider_settings=dict(provider_settings),
            )
            resolved = await _resolve(resolved)
        except TypeError:
            # Older test doubles/hosts may expose the same method without the
            # newer keyword-only provider_settings argument.
            try:
                resolved = await _resolve(
                    resolver(
                        umo=umo,
                        conversation_persona_id=_string_or_none(conversation_persona_id),
                        platform_name=str(platform_name),
                    )
                )
            except Exception:
                return None, None, ["selected persona could not be resolved"]
        except Exception:
            return None, None, ["selected persona could not be resolved"]

        if (
            not isinstance(resolved, Sequence)
            or isinstance(resolved, (str, bytes))
            or len(resolved) < 2
        ):
            return None, None, ["selected persona resolver returned an invalid value"]
        selected_id = _string_or_none(resolved[0])
        persona = resolved[1]
        prompt = _persona_prompt(persona)
        if prompt is None and persona is not None:
            warnings.append("selected persona has no prompt; persona metadata was retained")
        return prompt, selected_id, warnings

    async def _capture_media(
        self,
        event: Any,
        *,
        workspace: str | os.PathLike[str] | None,
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        components = _event_components(event)
        if not components:
            return [], [], []

        destination: Path | None = None
        if workspace is not None:
            destination = Path(workspace).expanduser().resolve(strict=False) / ".astrbot-media"
            try:
                destination.mkdir(parents=True, exist_ok=True)
            except OSError:
                destination = None

        media: list[dict[str, Any]] = []
        references: list[str] = []
        warnings: list[str] = []
        for index, component in enumerate(components):
            kind = _component_kind(component)
            if kind not in {"image", "record", "audio", "video", "file"}:
                continue

            source = _component_reference(component)
            resolved = await self._resolve_component_path(component, kind)
            reference = resolved or source
            local_path = _existing_local_path(reference)
            copied = _copy_media(local_path, destination, index, kind)
            if local_path and destination is not None and copied is None:
                warnings.append(f"media {index} could not be copied into task workspace")
            if copied:
                reference = copied
            elif local_path:
                # Never persist an AstrBot temp path.  Such files are cleaned
                # with the event and would leave a durable task unusable.
                reference = None
            elif not reference:
                warnings.append(f"media {index} has no usable path or URL reference")
                continue

            item = {
                "index": index,
                "kind": kind,
                "reference": reference,
                "source": source or None,
                "copied": bool(copied),
                "mime_type": mimetypes.guess_type(str(reference))[0],
            }
            if reference is None:
                continue
            media.append(item)
            references.append(reference)

        return media, references, warnings

    async def _resolve_component_path(self, component: Any, kind: str) -> str | None:
        direct = _existing_local_path(_component_reference(component))
        if direct:
            return direct

        method_name = "get_file" if kind == "file" else "convert_to_file_path"
        method = getattr(component, method_name, None)
        if not callable(method):
            return direct
        try:
            value = method()
            value = await asyncio.wait_for(
                _resolve(value),
                timeout=self.media_timeout_seconds,
            )
        except (asyncio.TimeoutError, OSError, ValueError, TypeError):
            return None
        except Exception:
            return None
        return _existing_local_path(value) or _string_or_none(value)

    async def _call_public(
        self,
        instance: Any,
        method_name: str,
        *args: Any,
        default: Any,
    ) -> Any:
        method = getattr(instance, method_name, None)
        if not callable(method):
            return default
        try:
            return await _resolve(method(*args))
        except Exception:
            return default


def _decode_history(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return [item for item in value]
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, list) else None


def _json_safe(value: Any) -> Any:
    """Clone public values into JSON-compatible, credential-safe data."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return sanitize_value(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    for method_name in ("model_dump", "dict", "to_dict", "toDict"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            dumped = method()
            if inspect.isawaitable(dumped):
                continue
            return _json_safe(dumped)
        except Exception:  # noqa: BLE001
            continue
    return sanitize_value(str(value))


def _event_components(event: Any) -> tuple[Any, ...]:
    getter = getattr(event, "get_messages", None)
    if callable(getter):
        try:
            value = getter()
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return tuple(value)
        except Exception:
            pass
    message_obj = getattr(event, "message_obj", None)
    value = getattr(message_obj, "message", ())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(value)
    return ()


def _component_kind(component: Any) -> str:
    raw = getattr(component, "type", None)
    raw = getattr(raw, "value", raw)
    if raw is None:
        raw = type(component).__name__
    value = str(raw).strip().lower()
    if value in {"record", "audio", "voice"}:
        return "record"
    if value in {"image", "img", "photo"}:
        return "image"
    if value in {"video", "movie"}:
        return "video"
    if value in {"file", "document", "attachment"}:
        return "file"
    return value


def _component_reference(component: Any) -> str | None:
    for name in ("path", "file_", "file", "url"):
        value = getattr(component, name, None)
        if callable(value):
            continue
        normalized = _string_or_none(value)
        if normalized:
            return normalized
    return None


def _existing_local_path(value: Any) -> str | None:
    normalized = _string_or_none(value)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if parsed.scheme == "file":
        normalized = unquote(parsed.path)
        if os.name == "nt" and normalized.startswith("/") and len(normalized) > 2 and normalized[2] == ":":
            normalized = normalized[1:]
    elif parsed.scheme:
        return None
    try:
        path = Path(normalized).expanduser()
        if path.is_file():
            return str(path.resolve())
    except OSError:
        return None
    return None


def _copy_media(
    source: str | None,
    destination: Path | None,
    index: int,
    kind: str,
) -> str | None:
    if not source or destination is None:
        return None
    try:
        source_path = Path(source).resolve(strict=True)
        if not source_path.is_file():
            return None
        suffix = source_path.suffix.lower()
        target = destination / f"media-{index:03d}-{kind}{suffix}"
        shutil.copy2(source_path, target)
        return str(target.resolve())
    except (OSError, ValueError):
        return None


def _persona_prompt(persona: Any) -> str | None:
    if isinstance(persona, Mapping):
        value = persona.get("prompt")
    else:
        value = getattr(persona, "prompt", None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _call(instance: Any, name: str, default: Any) -> Any:
    method = getattr(instance, name, None)
    if not callable(method):
        return default
    try:
        return method()
    except Exception:
        return default


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def _resolve(value: Awaitable[Any] | Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = ["AstrBotContextAdapter", "CapturedAstrBotContext"]
