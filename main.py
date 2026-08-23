import json
import inspect
import sys
from pathlib import Path
from typing import Any

# Ensure the sibling `pi_connector` package is importable when AstrBot loads
# this file directly as a standalone module.
sys.path.insert(0, str(Path(__file__).parent))

from pi_connector import PiConnection, PiConnectionManager, PiError
from pi_connector.commands import (
    extract_active_branch,
    format_commands_list,
    format_session_info,
    format_session_list,
    format_tree_entries,
    format_ui_request,
    parse_subcommand,
    parse_ui_reply_args,
    resolve_select_option,
    resolve_tree_entry_id,
    strip_command_prefix,
)
from pi_agent_bridge import (
    AstrBotAdapter,
    AstrBotContextAdapter,
    PiTaskService,
    TaskScheduler,
    TaskRegistry,
    capture_task_context,
)
from pi_agent_bridge.provider import (
    PiProviderError,
    build_provider_binding,
)
from pi_agent_bridge.runtime import PiRuntimeAdapter
from pi_agent_bridge.security import safe_error_summary
from pi_agent_bridge.worker import (
    PiWorkerConfig,
    WORKER_DESCRIPTOR_KEY,
    validate_resource_paths,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType
from astrbot.api.star import Context, Star

USAGE = """Pi Connector 命令帮助

会话管理：
  /pi open <绝对路径>       - 在指定目录打开新的 pi session
  /pi sessions [目录]         - 列出目录下的 session（省略则使用当前 session 目录，默认 10 条/页）
  /pi next                  - 查看 /pi sessions 的下一页
  /pi session               - 显示当前 session 信息
  /pi info                  - /pi session 的别名
  /pi resume [id]           - 恢复已有 session（省略 id 则恢复最近会话）
  /pi tree [编号]           - 查看当前会话 user-only 历史并编号；带编号可分叉
  /pi abort                 - 中止当前 pi 操作

对话与命令：
  /pi <自然语言>             - 向当前 session 发送消息
  /pic <command>            - 执行 pi 的 slash 命令（例如 /pic opsx-explore）
  /pic help                 - 列出当前 session 可用的 slash 命令

回复 pi 的 UI 请求：
  /pi confirm <id> yes|no
  /pi select <id> <选项或编号>
  /pi input <id> <内容>
  /pi edit <id> <内容>
  /pi cancel <id>

示例：
  /pi open /home/guigui/project
  /pi 帮我重构 auth 模块
  /pic help
  /pi sessions
"""


def _message_error(message: Any) -> str | None:
    """Extract a Pi provider/agent error from a message_end payload."""

    if not isinstance(message, dict):
        return None
    if message.get("stopReason") != "error" and not message.get("errorMessage"):
        return None
    value = message.get("errorMessage") or "Pi agent turn failed"
    return safe_error_summary(str(value))


class PiConnectorPlugin(Star):
    """Connect AstrBot to a local pi agent for session management, chat, and code tasks."""

    def __init__(self, context: Context, config=None):
        try:
            super().__init__(context, config=config)
        except TypeError:
            # Keep standalone test doubles and older AstrBot Star bases working.
            super().__init__(context)
        self.plugin_config = config if config is not None else {}
        self.astrbot_adapter = AstrBotAdapter(context, self.plugin_config)
        self.astrbot_context_adapter = AstrBotContextAdapter(
            context,
            media_timeout_seconds=float(
                self._config_value("media_capture_timeout_seconds", 10.0)
            ),
        )
        # Use pi's native session directory so sessions are shared with the
        # pi CLI and any other pi clients. This can be made configurable later
        # if per-plugin isolation is desired.
        self.pi_connection_manager = PiConnectionManager(
            session_dir=self._config_value("pi_session_dir", None) or None,
        )
        self.pi_task_service: PiTaskService | None = None
        self.pi_task_scheduler: TaskScheduler | None = None
        self._task_registry: TaskRegistry | None = None
        self._task_service_lock = None
        self._legacy_output_pages: dict[str, tuple[str, int]] = {}
        logger.info("PiConnector initialized")

    async def initialize(self):
        """Async initialization hook called after the Star is instantiated."""
        if self._config_bool("enable_async_tasks", True):
            await self._ensure_task_service()
        logger.info("PiConnector plugin initialized.")

    async def terminate(self):
        """Terminate all managed pi connections when the plugin is unloaded."""
        if self.pi_task_service is not None:
            await self.pi_task_service.shutdown()
            self.pi_task_service = None
        if self.pi_task_scheduler is not None:
            await self.pi_task_scheduler.shutdown()
            self.pi_task_scheduler = None
        if self._task_registry is not None:
            self._task_registry.close()
            self._task_registry = None
        await self.pi_connection_manager.terminate_all()

    def _config_value(self, key: str, default):
        getter = getattr(self.plugin_config, "get", None)
        return getter(key, default) if callable(getter) else default

    def _config_bool(self, key: str, default: bool) -> bool:
        """Normalize bool config values supplied as native values or strings."""
        value = self._config_value(key, default)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off", ""}:
                return False
        return bool(value)

    async def _ensure_task_service(self) -> PiTaskService:
        """Create the background bridge lazily and start its observer."""
        if self.pi_task_service is not None:
            return self.pi_task_service

        # Import lazily so plugin construction stays compatible with hosts
        # that instantiate plugins outside an active event loop.
        import asyncio

        if self._task_service_lock is None:
            self._task_service_lock = asyncio.Lock()
        async with self._task_service_lock:
            if self.pi_task_service is not None:
                return self.pi_task_service
            return await self._create_task_service()

    async def _create_task_service(self) -> PiTaskService:
        """Build and publish one registry/scheduler/service triplet."""

        configured_state = self._config_value("state_directory", "")
        state_root = (
            Path(configured_state).expanduser()
            if configured_state
            else self._default_state_root()
        )
        state_root.mkdir(parents=True, exist_ok=True)

        database = self._config_value("task_database", "")
        if not database:
            database = str(state_root / "tasks.db")
        registry = TaskRegistry(database)
        workspace_root = self._config_value("workspace_root", "")
        if not workspace_root:
            workspace_root = str(state_root / "workspaces")

        runtime_adapter = PiRuntimeAdapter(
            plugin_root=Path(__file__).parent,
            configured_command=None,
        )
        agent_root = state_root / "agents"
        # The provider and model are captured with each task so every worker
        # uses the one fixed plugin configuration, never the current chat model.
        configured_skill_paths = self._config_value("pi_skill_paths", [])
        configured_extension_paths = self._config_value("pi_extension_paths", [])

        def configured_skill_paths_for_worker() -> tuple[str, ...]:
            return validate_resource_paths(
                configured_skill_paths,
                label="pi_skill_paths",
                require_exists=True,
            )

        def configured_extension_paths_for_worker() -> tuple[str, ...]:
            return validate_resource_paths(
                configured_extension_paths,
                label="pi_extension_paths",
                require_exists=True,
            )

        async def worker_config_factory(task) -> PiWorkerConfig:
            descriptor = task.context.get(WORKER_DESCRIPTOR_KEY, {})
            if not isinstance(descriptor, dict):
                raise PiProviderError("Pi task provider descriptor is invalid")
            source_id = str(descriptor.get("source_provider_id") or "").strip()
            if not source_id:
                raise PiProviderError(
                    "The fixed pi_model configuration must select an AstrBot provider/model"
                )
            getter = getattr(self.astrbot_adapter.context, "get_provider_by_id", None)
            if not callable(getter):
                raise PiProviderError("AstrBot provider lookup API is unavailable")
            provider = getter(source_id)
            if inspect.isawaitable(provider):
                provider = await provider
            if provider is None:
                raise PiProviderError(f"AstrBot provider {source_id!r} is unavailable")
            agent_dir = agent_root / task.task_id
            binding = build_provider_binding(
                provider_id=source_id,
                provider=provider,
                agent_dir=agent_dir,
            )
            return PiWorkerConfig(
                provider=binding.pi_provider_id,
                model=binding.model,
                environment=binding.environment,
                agent_dir=binding.agent_dir,
                skill_paths=configured_skill_paths_for_worker(),
                extension_paths=configured_extension_paths_for_worker(),
            )

        async def notify_task_update(task, _snapshot) -> None:
            """Send one concise actionable notification, never streaming progress."""

            if not self._config_bool("notify_task_completion", True):
                return
            labels = {
                "completed": "已完成",
                "failed": "失败",
                "cancelled": "已取消",
                "needs_user_decision": "等待你的决定",
            }
            status = labels.get(task.status.value, task.status.value)
            try:
                await self.astrbot_adapter.send_text(
                    task.owner_key,
                    f"Pi 后台任务{status}。\n任务 ID：{task.task_id}\n"
                    "请使用 pi_task_result 查看结果，或使用对应任务工具继续操作。",
                )
            except Exception:  # noqa: BLE001
                logger.debug("Unable to notify task owner for %s", task.task_id, exc_info=True)

        try:
            scheduler = TaskScheduler(
                registry,
                workspace_root=workspace_root,
                executable="pi",
                runtime_adapter=runtime_adapter,
                agent_root=agent_root,
                worker_config_factory=worker_config_factory,
                poll_interval_seconds=int(
                    self._config_value("poll_interval_seconds", 60)
                ),
                no_meaningful_event_limit=int(
                    self._config_value("no_meaningful_event_limit", 3)
                ),
                max_concurrent_tasks=int(
                    self._config_value("max_concurrent_tasks", 4)
                ),
                command_timeout=float(
                    self._config_value("command_timeout_seconds", 10.0)
                ),
                session_retention_hours=float(
                    self._config_value("session_retention_hours", 24)
                ),
                session_root=state_root / "sessions",
                task_update_callback=notify_task_update,
            )
            await scheduler.start()
        except Exception:
            registry.close()
            raise
        self._task_registry = registry
        self.pi_task_scheduler = scheduler
        self.pi_task_service = PiTaskService(registry, scheduler)
        return self.pi_task_service

    @staticmethod
    def _default_state_root() -> Path:
        """Use AstrBot's plugin-data directory, never the source checkout."""

        try:
            from astrbot.api.star import StarTools

            getter = getattr(StarTools, "get_data_dir", None)
            if callable(getter):
                return Path(getter("astrbot_plugin_pi_agent"))
        except Exception:  # noqa: BLE001
            # Standalone tests and older hosts may not expose StarTools yet.
            pass
        return Path(__file__).with_name(".pi")

    async def _task_service_or_error(self) -> PiTaskService:
        if not self._config_bool("enable_async_tasks", True):
            raise RuntimeError("Pi task bridge is disabled in plugin configuration")
        return await self._ensure_task_service()

    # ------------------------------------------------------------------
    # Permission check
    # ------------------------------------------------------------------

    def _require_admin(self, event: AstrMessageEvent) -> str | None:
        """Return a permission-denied message if the sender is not an AstrBot admin."""
        if not event.is_admin():
            return "Permission denied. Pi Connector is only available to AstrBot administrators."
        return None

    def _require_task_permission(self, event: AstrMessageEvent) -> str | None:
        """Keep async task access owner-scoped; the flag only grants admin-wide access."""
        return None

    def _can_manage_all_tasks(self, event: AstrMessageEvent) -> bool:
        """Return whether this admin may manage tasks owned by other users."""
        return self._config_bool("task_require_admin", False) and event.is_admin()

    def _task_owner_key(self, event: AstrMessageEvent) -> str:
        return capture_task_context(event).owner_key

    def _task_is_visible(self, event: AstrMessageEvent, task) -> bool:
        return task.owner_key == self._task_owner_key(event) or self._can_manage_all_tasks(event)

    def _legacy_output_key(self, event: AstrMessageEvent) -> str:
        try:
            return self.astrbot_adapter.session_origin(event)
        except Exception:  # noqa: BLE001
            return str(id(event))

    def _paginate_legacy_output(self, event: AstrMessageEvent, output: str) -> str:
        page_size = 4000
        key = self._legacy_output_key(event)
        if len(output) <= page_size:
            self._legacy_output_pages.pop(key, None)
            return output
        self._legacy_output_pages[key] = (output, page_size)
        return (
            output[:page_size]
            + "\n\n[输出已截断，调用 pi_legacy_output_next 获取下一页]"
        )

    def _next_legacy_output_page(self, event: AstrMessageEvent) -> str:
        key = self._legacy_output_key(event)
        state = self._legacy_output_pages.get(key)
        if state is None:
            return "No paged legacy Pi output is available."
        output, offset = state
        page_size = 4000
        page = output[offset : offset + page_size]
        next_offset = offset + len(page)
        if next_offset >= len(output):
            self._legacy_output_pages.pop(key, None)
            return page
        self._legacy_output_pages[key] = (output, next_offset)
        return page + "\n\n[输出未结束，继续调用 pi_legacy_output_next]"

    @staticmethod
    def _bridge_error(
        operation: str,
        message: str,
        *,
        task_id: str | None = None,
    ) -> str:
        """Serialize bridge failures for the calling model, never for users."""

        return json.dumps(
            {
                "schema_version": "1",
                "ok": False,
                "operation": operation,
                "task_id": task_id,
                "status": None,
                "has_new_meaningful_event": False,
                "progress": {},
                "content": [],
                "artifacts": [],
                "error": {
                    "type": "pi_task_error",
                    "message": safe_error_summary(message),
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _bridge_dump(result: dict[str, Any]) -> str:
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    def _visible_task(self, event: AstrMessageEvent, task_id: str):
        """Return a task only when the caller owns it or is an administrator."""

        if self._task_registry is None:
            return None
        try:
            task = self._task_registry.get_task(task_id)
        except Exception:  # noqa: BLE001
            return None
        return task if self._task_is_visible(event, task) else None

    async def _service_for_task(
        self, event: AstrMessageEvent, task_id: str
    ) -> PiTaskService:
        """Load the bridge before enforcing task visibility.

        Lazy initialization means the registry is unavailable until the first
        tool call. Keeping initialization and authorization together prevents
        disabled-bridge calls from being misreported as missing tasks.
        """

        service = await self._task_service_or_error()
        if self._visible_task(event, task_id) is None:
            raise LookupError(
                "task not found or legacy session id supplied; use pi_send_message "
                "for pi_open_session sessions, or use the task_id returned by pi_agent "
                "with pi_task_* tools"
            )
        return service

    def _current_persona(self, event: AstrMessageEvent) -> str | None:
        """Best-effort persona snapshot using the public context/config surface."""

        context = self.astrbot_adapter.context
        manager = getattr(context, "persona_manager", None)
        if manager is None:
            return None
        try:
            umo = getattr(event, "unified_msg_origin", "")
            if callable(umo):
                umo = umo()
            config = context.get_config(umo=umo) if umo else context.get_config()
            settings = config.get("provider_settings", {}) if config else {}
            persona_id = settings.get("default_personality")
            getter = getattr(manager, "get_persona_v3_by_id", None)
            persona = getter(persona_id) if callable(getter) else None
            if isinstance(persona, dict):
                prompt = persona.get("prompt")
            else:
                prompt = getattr(persona, "prompt", None)
            return prompt.strip() if isinstance(prompt, str) and prompt.strip() else None
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Command parsing helpers
    # ------------------------------------------------------------------

    async def _get_connection(self, event: AstrMessageEvent) -> PiConnection:
        """Return the active connection for the chat or raise PiError."""
        conn = await self.pi_connection_manager.get_connection(event, create=False)
        if conn is None or conn.process is None:
            raise PiError("No active pi session. Use /pi open or /pi resume first.")
        return conn

    async def _stream_events(
        self,
        event: AstrMessageEvent,
        event_generator,
        buffer_size: int = 100,
    ):
        """Consume a pi event generator and yield plain text results.

        Text deltas are accumulated and yielded once they reach ``buffer_size``
        characters. When a pi extension UI request is encountered, the accumulated
        text is flushed and the UI request is displayed to the user.
        """
        buffer = ""
        async for ev in event_generator:
            ev_type = ev.get("type")
            if ev_type == "text":
                buffer += ev.get("text", "")
                if len(buffer) >= buffer_size:
                    yield event.plain_result(buffer)
                    buffer = ""
            elif ev_type == "thinking":
                # Thinking is intentionally not shown to keep the chat clean.
                pass
            elif ev_type == "ui_request":
                if buffer:
                    yield event.plain_result(buffer)
                    buffer = ""
                ui_request = ev.get("request")
                if ui_request:
                    yield event.plain_result(format_ui_request(ui_request))
                return

        if buffer:
            yield event.plain_result(buffer)

    async def _collect_events(self, generator) -> str:
        """Collect all text events from a pi event generator until agent_end.

        Tool execution events and UI requests are reported inline so the
        caller can see what happened.
        """
        parts = []
        async for ev in generator:
            ev_type = ev.get("type")
            if ev_type == "text":
                parts.append(ev.get("text", ""))
            elif ev_type == "ui_request":
                request = ev.get("request")
                if request:
                    parts.append("\n" + format_ui_request(request) + "\n")
                    return "".join(parts)
            elif ev_type == "event":
                raw = ev.get("event", {})
                if raw.get("type") == "message_end":
                    message = raw.get("message")
                    error = _message_error(message)
                    if error:
                        parts.append(f"[Pi error] {error}\n")
                elif raw.get("type") == "agent_end":
                    for message in raw.get("messages", []):
                        error = _message_error(message)
                        if error:
                            parts.append(f"[Pi error] {error}\n")
                elif raw.get("type") == "tool_execution_start":
                    parts.append(
                        f"\n[Tool: {raw.get('toolName')}({raw.get('args', {})})]\n"
                    )
                elif raw.get("type") == "tool_execution_end":
                    result = raw.get("result", {})
                    content = result.get("content", [])
                    text = ""
                    for block in content:
                        if block.get("type") == "text":
                            text += block.get("text", "")
                    parts.append(f"[Tool result]: {text}\n")
        return "".join(parts)

    async def _collect_prompt_response(self, conn: PiConnection, prompt: str) -> str:
        """Send a prompt and collect all text events until agent_end."""
        generator = conn.send_prompt(prompt)
        result = await self._collect_events(generator)
        if not result.strip():
            raise PiError(
                "Pi completed without a response; no file operation was confirmed"
            )
        return result

    # ------------------------------------------------------------------
    # /pi command handlers
    # ------------------------------------------------------------------

    @filter.command("pi")
    @filter.permission_type(PermissionType.ADMIN)
    async def pi_handler(self, event: AstrMessageEvent):
        """Dispatch /pi subcommands or treat the message as a natural language prompt."""
        text = strip_command_prefix(event.message_str, "pi")
        subcommand, rest = parse_subcommand(text)

        if subcommand == "open":
            async for item in self._handle_pi_open(event, rest):
                yield item
        elif subcommand == "sessions":
            async for item in self._handle_pi_sessions(event, rest):
                yield item
        elif subcommand == "next":
            async for item in self._handle_pi_next(event):
                yield item
        elif subcommand == "session":
            async for item in self._handle_pi_session_info(event):
                yield item
        elif subcommand == "resume":
            async for item in self._handle_pi_resume(event, rest):
                yield item
        elif subcommand == "tree":
            async for item in self._handle_pi_tree(event, rest):
                yield item
        elif subcommand == "confirm":
            async for item in self._handle_pi_confirm(event, rest):
                yield item
        elif subcommand == "select":
            async for item in self._handle_pi_select(event, rest):
                yield item
        elif subcommand == "input":
            async for item in self._handle_pi_input(event, rest):
                yield item
        elif subcommand == "edit":
            async for item in self._handle_pi_edit(event, rest):
                yield item
        elif subcommand == "cancel":
            async for item in self._handle_pi_cancel(event, rest):
                yield item
        elif subcommand == "abort":
            async for item in self._handle_pi_abort(event):
                yield item
        elif subcommand == "info":
            async for item in self._handle_pi_session_info(event):
                yield item
        elif subcommand in ("help", ""):
            yield event.plain_result(USAGE)
        else:
            # Treat the entire stripped text as a natural language prompt.
            async for item in self._handle_pi_prompt(event, text):
                yield item

    async def _handle_pi_open(self, event: AstrMessageEvent, rest: str):
        """Handle /pi open <absolute path>."""
        path = rest.strip()
        if not path:
            yield event.plain_result("Usage: /pi open <absolute path>")
            return
        try:
            info = await self.pi_connection_manager.open_session(event, path)
            yield event.plain_result(
                f"Opened new pi session.\n{format_session_info(info)}"
            )
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    async def _handle_pi_sessions(self, event: AstrMessageEvent, rest: str):
        """Handle /pi sessions [dir]."""
        directory = rest.strip() or None
        if directory:
            self.pi_connection_manager.set_active_cwd(event, directory)
        else:
            directory = await self.pi_connection_manager.get_active_cwd(event)
            if not directory:
                yield event.plain_result(
                    "Usage: /pi sessions <absolute directory>\n"
                    "Open or resume a session first to list its directory."
                )
                return

        page_size = 10
        try:
            sessions, total = self.pi_connection_manager.list_sessions(
                directory, offset=0, limit=page_size
            )
            self.pi_connection_manager.set_last_sessions_query(
                event,
                directory=directory,
                page=1,
                page_size=page_size,
                total=total,
            )
            yield event.plain_result(
                format_session_list(
                    sessions,
                    directory=directory,
                    page=1,
                    page_size=page_size,
                    total=total,
                )
            )
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    async def _handle_pi_next(self, event: AstrMessageEvent):
        """Handle /pi next: show the next page of the last sessions query."""
        query = self.pi_connection_manager.get_last_sessions_query(event)
        if not query:
            yield event.plain_result(
                "No previous sessions list. Use /pi sessions <directory> first."
            )
            return

        directory = query["directory"]
        page = query["page"] + 1
        page_size = query["page_size"]
        offset = (page - 1) * page_size
        try:
            sessions, total = self.pi_connection_manager.list_sessions(
                directory, offset=offset, limit=page_size
            )
            if not sessions:
                yield event.plain_result("No more sessions.")
                return
            self.pi_connection_manager.set_last_sessions_query(
                event,
                directory=directory,
                page=page,
                page_size=page_size,
                total=total,
            )
            yield event.plain_result(
                format_session_list(
                    sessions,
                    directory=directory,
                    page=page,
                    page_size=page_size,
                    total=total,
                )
            )
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    async def _handle_pi_session_info(self, event: AstrMessageEvent):
        """Handle /pi session (show current session info)."""
        try:
            info = await self.pi_connection_manager.get_session_info(event)
            yield event.plain_result(
                f"Current pi session:\n{format_session_info(info)}"
            )
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    async def _handle_pi_resume(self, event: AstrMessageEvent, rest: str):
        """Handle /pi resume [id]. Without id, resume the most recent session."""
        session_id = rest.strip() or None
        try:
            info = await self.pi_connection_manager.resume_session(event, session_id)
            source = f"session {session_id}" if session_id else "most recent session"
            yield event.plain_result(f"Resumed {source}.\n{format_session_info(info)}")
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    async def _handle_pi_tree(self, event: AstrMessageEvent, rest: str):
        """Handle /pi tree and /pi tree <number>."""
        rest = rest.strip()
        try:
            conn = await self._get_connection(event)
            tree_data = await conn.get_tree()
            tree = tree_data.get("tree", [])
            leaf_id = tree_data.get("leafId")
            branch_entries = extract_active_branch(tree, leaf_id)

            if not rest:
                yield event.plain_result(format_tree_entries(branch_entries))
                return

            try:
                number = int(rest)
            except ValueError:
                yield event.plain_result(
                    "Usage: /pi tree <number>\nPlease provide a valid number."
                )
                return

            entry_id = resolve_tree_entry_id(branch_entries, number)
            if entry_id is None:
                yield event.plain_result(
                    f"Invalid number {number}. Use `/pi tree` to see available entries."
                )
                return

            result = await conn.fork(entry_id)
            if result.get("cancelled"):
                yield event.plain_result("Fork was cancelled by an extension.")
                return

            info = await self.pi_connection_manager.get_session_info(event)
            yield event.plain_result(
                f"Forked from entry {number}.\n{format_session_info(info)}\n"
                "You can now continue chatting from this point."
            )
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    async def _handle_pi_prompt(self, event: AstrMessageEvent, text: str):
        """Handle /pi <natural language>."""
        if not text.strip():
            yield event.plain_result(USAGE)
            return
        try:
            conn = await self._get_connection(event)
            event_generator = conn.send_prompt(text)
            async for item in self._stream_events(event, event_generator):
                yield item
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    async def _handle_pi_abort(self, event: AstrMessageEvent):
        """Handle /pi abort."""
        try:
            conn = await self._get_connection(event)
            await conn.abort()
            yield event.plain_result("Abort request sent to pi.")
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    # ------------------------------------------------------------------
    # UI reply handlers
    # ------------------------------------------------------------------

    async def _handle_pi_confirm(self, event: AstrMessageEvent, rest: str):
        """Handle /pi confirm <id> yes|no."""
        local_id, value = parse_ui_reply_args(rest)
        if local_id is None:
            yield event.plain_result("Usage: /pi confirm <id> yes|no")
            return
        if not value.strip():
            yield event.plain_result(
                "Usage: /pi confirm <id> yes|no\nPlease specify yes or no."
            )
            return
        confirmed = value.strip().lower() in ("yes", "y", "true", "1")
        try:
            conn = await self._get_connection(event)
            ui_request = conn.get_ui_request_by_local_id(local_id)
            if ui_request is None:
                yield event.plain_result(f"No pending request with id {local_id}.")
                return
            await conn.confirm_ui_request(ui_request.request_id, confirmed)
            yield event.plain_result(
                f"{'Confirmed' if confirmed else 'Declined'} request #{local_id}."
            )
            async for item in self._stream_events(event, conn.read_response()):
                yield item
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    async def _handle_pi_select(self, event: AstrMessageEvent, rest: str):
        """Handle /pi select <id> <option or number>."""
        local_id, value = parse_ui_reply_args(rest)
        if local_id is None:
            yield event.plain_result("Usage: /pi select <id> <option or number>")
            return
        try:
            conn = await self._get_connection(event)
            ui_request = conn.get_ui_request_by_local_id(local_id)
            if ui_request is None:
                yield event.plain_result(f"No pending request with id {local_id}.")
                return
            selected = resolve_select_option(ui_request, value)
            if selected is None:
                yield event.plain_result(
                    f"Invalid option '{value}'. Use the option text or a 1-based number."
                )
                return
            await conn.reply_ui_request(ui_request.request_id, selected)
            yield event.plain_result(f"Selected '{selected}' for request #{local_id}.")
            async for item in self._stream_events(event, conn.read_response()):
                yield item
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    async def _handle_pi_input(self, event: AstrMessageEvent, rest: str):
        """Handle /pi input <id> <value>."""
        local_id, value = parse_ui_reply_args(rest)
        if local_id is None:
            yield event.plain_result("Usage: /pi input <id> <value>")
            return
        try:
            conn = await self._get_connection(event)
            ui_request = conn.get_ui_request_by_local_id(local_id)
            if ui_request is None:
                yield event.plain_result(f"No pending request with id {local_id}.")
                return
            await conn.reply_ui_request(ui_request.request_id, value)
            yield event.plain_result(f"Replied to request #{local_id}.")
            async for item in self._stream_events(event, conn.read_response()):
                yield item
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    async def _handle_pi_edit(self, event: AstrMessageEvent, rest: str):
        """Handle /pi edit <id> <text>."""
        local_id, value = parse_ui_reply_args(rest)
        if local_id is None:
            yield event.plain_result("Usage: /pi edit <id> <text>")
            return
        try:
            conn = await self._get_connection(event)
            ui_request = conn.get_ui_request_by_local_id(local_id)
            if ui_request is None:
                yield event.plain_result(f"No pending request with id {local_id}.")
                return
            await conn.reply_ui_request(ui_request.request_id, value)
            yield event.plain_result(f"Edited text for request #{local_id}.")
            async for item in self._stream_events(event, conn.read_response()):
                yield item
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    async def _handle_pi_cancel(self, event: AstrMessageEvent, rest: str):
        """Handle /pi cancel <id>."""
        local_id, _ = parse_ui_reply_args(rest)
        if local_id is None:
            yield event.plain_result("Usage: /pi cancel <id>")
            return
        try:
            conn = await self._get_connection(event)
            ui_request = conn.get_ui_request_by_local_id(local_id)
            if ui_request is None:
                yield event.plain_result(f"No pending request with id {local_id}.")
                return
            await conn.cancel_ui_request(ui_request.request_id)
            yield event.plain_result(f"Cancelled request #{local_id}.")
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    # ------------------------------------------------------------------
    # /pic command handlers
    # ------------------------------------------------------------------

    @filter.command("pic")
    @filter.permission_type(PermissionType.ADMIN)
    async def pic_handler(self, event: AstrMessageEvent):
        """Handle /pic <command> and /pic help."""
        text = strip_command_prefix(event.message_str, "pic")
        if not text.strip():
            yield event.plain_result("Usage: /pic <command> or /pic help")
            return

        if text.strip().lower() == "help":
            async for item in self._handle_pic_help(event):
                yield item
            return

        command = text.strip()
        if not command.startswith("/"):
            command = f"/{command}"

        try:
            conn = await self._get_connection(event)
            event_generator = conn.send_prompt(command)
            async for item in self._stream_events(event, event_generator):
                yield item
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    async def _handle_pic_help(self, event: AstrMessageEvent):
        """Handle /pic help."""
        try:
            conn = await self._get_connection(event)
            commands = await conn.get_commands()
            yield event.plain_result(format_commands_list(commands))
        except PiError as exc:
            yield event.plain_result(f"Error: {exc}")

    # ------------------------------------------------------------------
    # LLM tools
    # ------------------------------------------------------------------

    @filter.llm_tool(name="pi_agent")
    async def pi_agent(
        self,
        event: AstrMessageEvent,
        prompt: str,
        workspace: str = "",
    ) -> str:
        """Delegate a long-running, multi-step task to an isolated Pi worker.

        Use this for sustained research, coding, multi-agent work, or tasks
        that should continue while the main conversation handles other turns.
        The call returns immediately with a task id; use the task tools to
        observe it. Simple questions and short tool calls belong to AstrBot.

        Args:
            prompt(string): Complete task instruction for the delegated worker
            workspace(string): Optional absolute workspace path for the task
        """
        operation = "task_create"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied)
        try:
            model_config = self._config_value("pi_model", "")
            provider_id = str(model_config or "").strip()
            if not provider_id:
                return self._bridge_error(
                    operation,
                    "请先在 pi_model 中选择 AstrBot 已配置的具体模型",
                )
            service = await self._task_service_or_error()
            context = capture_task_context(event)
            mcp_paths = self._config_value("pi_mcp_config_paths", [])
            if mcp_paths:
                return self._bridge_error(
                    operation,
                    "Pi MCP integration is unsupported by the bundled Pi RPC bridge; "
                    "configure a separately maintained Pi extension first",
                )
            descriptor = {
                "source_provider_id": provider_id,
            }

            # Capture the full context only after ``PiTaskService`` has chosen
            # the final task workspace.  This keeps event-owned media alive
            # after AstrBot's pipeline disposes its temporary files while
            # still returning immediately from the model tool call.
            initial_context = context
            initial_context_data = initial_context.as_dict()
            initial_context_data[WORKER_DESCRIPTOR_KEY] = dict(descriptor)
            inherit_persona = self._config_bool("inherit_persona", True)
            # Keep test doubles and host integrations that replace the public
            # context object after plugin construction aligned with capture.
            self.astrbot_context_adapter.context = self.astrbot_adapter.context

            async def prepare_context(_task_id: str, task_workspace: Path):
                captured = await self.astrbot_context_adapter.capture(
                    event,
                    workspace=task_workspace,
                    inherit_persona=inherit_persona,
                )
                prepared = captured.as_dict()
                prepared[WORKER_DESCRIPTOR_KEY] = dict(descriptor)
                return prepared

            result = await service.create_task(
                owner_key=initial_context.owner_key,
                task=prompt,
                context=initial_context_data,
                persona=None,
                media_references=[],
                workspace=workspace or None,
                prepare_context=prepare_context,
            )
            return self._bridge_dump(result)
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc))

    @filter.llm_tool(name="pi_task_status")
    async def pi_task_status(self, event: AstrMessageEvent, task_id: str) -> str:
        """Read the latest durable state and snapshot for a Pi task.

        Args:
            task_id(string): Task id returned by pi_agent
        """
        operation = "task_status"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id)
            return self._bridge_dump(service.status(task_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_task_list")
    async def pi_task_list(self, event: AstrMessageEvent) -> str:
        """List Pi tasks visible to this conversation owner.

        Administrators receive all tasks; other callers receive their own.
        """
        operation = "task_list"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied)
        try:
            service = await self._task_service_or_error()
            owner = None if self._can_manage_all_tasks(event) else self._task_owner_key(event)
            return self._bridge_dump(service.list_tasks(owner_key=owner))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc))

    @filter.llm_tool(name="pi_task_result")
    async def pi_task_result(
        self, event: AstrMessageEvent, task_id: str, offset: int = 0, limit: int = 4
    ) -> str:
        """Read a bounded page of task output and persisted artifacts.

        Args:
            task_id(string): Task id returned by pi_agent
            offset(number): Zero-based result block offset
            limit(number): Maximum result blocks to return
        """
        operation = "task_result"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id)
            return self._bridge_dump(service.result(task_id, offset=offset, limit=limit))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_task_poll")
    async def pi_task_poll(self, event: AstrMessageEvent, task_id: str) -> str:
        """Take one short, nonblocking observation of a Pi task.

        Args:
            task_id(string): Task id returned by pi_agent
        """
        operation = "task_poll"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id)
            return self._bridge_dump(await service.poll(task_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_task_follow_up")
    async def pi_task_follow_up(
        self, event: AstrMessageEvent, task_id: str, message: str
    ) -> str:
        """Inject an additional requirement into the active Pi task.

        Args:
            task_id(string): Task id returned by pi_agent
            message(string): Additional instruction to steer the worker
        """
        operation = "task_follow_up"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id)
            return self._bridge_dump(await service.follow_up(task_id, message))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_task_resume")
    async def pi_task_resume(self, event: AstrMessageEvent, task_id: str) -> str:
        """Resume a task paused after repeated empty observations.

        Args:
            task_id(string): Task id returned by pi_agent
        """
        operation = "task_resume"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id)
            return self._bridge_dump(await service.resume(task_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_task_cancel")
    async def pi_task_cancel(self, event: AstrMessageEvent, task_id: str) -> str:
        """Cancel a Pi task without deleting its durable history.

        Args:
            task_id(string): Task id returned by pi_agent
        """
        operation = "task_cancel"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id)
            return self._bridge_dump(await service.cancel(task_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_task_delete")
    async def pi_task_delete(self, event: AstrMessageEvent, task_id: str) -> str:
        """Cancel and delete a Pi task, metadata, and managed workspace.

        Args:
            task_id(string): Task id returned by pi_agent
        """
        operation = "task_delete"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id)
            return self._bridge_dump(await service.delete(task_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_session_list")
    async def pi_session_list(self, event: AstrMessageEvent) -> str:
        """List Pi sessions represented by visible background tasks."""
        operation = "session_list"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied)
        try:
            service = await self._task_service_or_error()
            owner = None if self._can_manage_all_tasks(event) else self._task_owner_key(event)
            result = service.session_list(owner_key=owner)
            if event.is_admin():
                legacy, total = self.pi_connection_manager.list_sessions()
                result["progress"]["legacy_sessions"] = [
                    {"resource_type": "legacy_session", **info.__dict__}
                    for info in legacy
                ]
                result["progress"]["legacy_count"] = total
            return self._bridge_dump(result)
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc))

    @filter.llm_tool(name="pi_session_inspect")
    async def pi_session_inspect(self, event: AstrMessageEvent, session_id: str) -> str:
        """Inspect either a task-owned session or an administrator legacy Pi session.

        Args:
            session_id(string): Task ID from pi_agent or session ID from pi_open_session
        """
        operation = "session_inspect"
        task = self._visible_task(event, session_id)
        if task is not None:
            try:
                service = await self._task_service_or_error()
                return self._bridge_dump(service.session_inspect(session_id))
            except Exception as exc:  # noqa: BLE001
                return self._bridge_error(operation, safe_error_summary(exc), task_id=session_id)
        if denied := self._require_admin(event):
            return self._bridge_error(operation, denied, task_id=session_id)
        try:
            info = self.pi_connection_manager.inspect_session(session_id)
            return self._bridge_dump(
                {
                    "schema_version": "1",
                    "ok": True,
                    "operation": operation,
                    "task_id": None,
                    "status": "legacy_session",
                    "has_new_meaningful_event": False,
                    "progress": {"session": info.__dict__},
                    "content": [],
                    "artifacts": [],
                    "error": None,
                }
            )
        except PiError as exc:
            return self._bridge_error(operation, str(exc), task_id=session_id)

    @filter.llm_tool(name="pi_session_resume")
    async def pi_session_resume(self, event: AstrMessageEvent, task_id: str) -> str:
        """Resume the Pi session for a paused or orphaned task.

        Args:
            task_id(string): Task id returned by pi_agent
        """
        operation = "session_resume"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id)
            return self._bridge_dump(await service.session_resume(task_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_session_delete")
    async def pi_session_delete(self, event: AstrMessageEvent, session_id: str) -> str:
        """Delete either a task-owned session or an administrator legacy Pi session.

        Args:
            session_id(string): Task ID from pi_agent or session ID from pi_open_session
        """
        operation = "session_delete"
        task = self._visible_task(event, session_id)
        if task is not None:
            try:
                service = await self._task_service_or_error()
                return self._bridge_dump(await service.session_delete(session_id))
            except Exception as exc:  # noqa: BLE001
                return self._bridge_error(operation, safe_error_summary(exc), task_id=session_id)
        if denied := self._require_admin(event):
            return self._bridge_error(operation, denied, task_id=session_id)
        try:
            await self.pi_connection_manager.delete_session(event, session_id)
            return self._bridge_dump(
                {
                    "schema_version": "1",
                    "ok": True,
                    "operation": operation,
                    "task_id": None,
                    "status": "legacy_session_deleted",
                    "has_new_meaningful_event": False,
                    "progress": {},
                    "content": [],
                    "artifacts": [],
                    "error": None,
                }
            )
        except PiError as exc:
            return self._bridge_error(operation, str(exc), task_id=session_id)

    @filter.llm_tool(name="pi_artifact_inspect")
    async def pi_artifact_inspect(self, event: AstrMessageEvent, task_id: str) -> str:
        """Inspect text, structured, and media artifacts produced by a task.

        Args:
            task_id(string): Task id returned by pi_agent
        """
        operation = "artifact_inspect"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id)
            return self._bridge_dump(service.artifact_inspect(task_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    # Backwards-compatible alias for older prompts and skills.
    @filter.llm_tool(name="pi_submit_task")
    async def pi_submit_task(
        self, event: AstrMessageEvent, prompt: str, workspace: str = ""
    ) -> str:
        """Deprecated alias for pi_agent.

        Args:
            prompt(string): Complete task instruction for Pi
            workspace(string): Optional absolute workspace path for the task
        """
        return await self.pi_agent(event, prompt, workspace)

    @filter.llm_tool(name="pi_open_session")
    async def pi_open_session(
        self, event: AstrMessageEvent, path: str, name: str = None
    ) -> str:
        """Open a new pi session at an absolute directory path.

        Args:
            path(string): Absolute directory path for the pi session
            name(string): Optional display name for the session
        """
        if denied := self._require_admin(event):
            return denied
        try:
            info = await self.pi_connection_manager.open_session(event, path, name=name)
            return (
                "Opened new pi session (legacy). Use pi_send_message, "
                "pi_get_session_info, or pi_resume_session with this session; "
                "do not pass this session id to pi_task_* tools.\n"
                f"{format_session_info(info)}"
            )
        except PiError as exc:
            return f"Error: {exc}"

    @filter.llm_tool(name="pi_list_sessions")
    async def pi_list_sessions(self, event: AstrMessageEvent, dir: str = None) -> str:
        """List existing pi sessions in a directory.

        Args:
            dir(string): Absolute directory to list sessions for. Uses the active session's directory if omitted.
        """
        if denied := self._require_admin(event):
            return denied
        try:
            directory = dir
            if not directory:
                directory = await self.pi_connection_manager.get_active_cwd(event)
            if not directory:
                return (
                    "No directory specified and no active session. "
                    "Provide a directory or open/resume a session first."
                )
            sessions, total = self.pi_connection_manager.list_sessions(directory)
            return format_session_list(
                sessions,
                directory=directory,
                page=1,
                page_size=len(sessions),
                total=total,
            )
        except PiError as exc:
            return f"Error: {exc}"

    @filter.llm_tool(name="pi_resume_session")
    async def pi_resume_session(
        self, event: AstrMessageEvent, session_id: str = ""
    ) -> str:
        """Resume an existing pi session by its id, file path, or the most recent.

        Args:
            session_id(string): Session id or partial id to resume. Omit to resume the most recent session.
        """
        if denied := self._require_admin(event):
            return denied
        try:
            info = await self.pi_connection_manager.resume_session(
                event, session_id or None
            )
            source = f"session {session_id}" if session_id else "most recent session"
            return f"Resumed {source}.\n{format_session_info(info)}"
        except PiError as exc:
            return f"Error: {exc}"

    @filter.llm_tool(name="pi_send_message")
    async def pi_send_message(self, event: AstrMessageEvent, message: str) -> str:
        """Send a natural language message to the current pi session.

        Args:
            message(string): The message to send to pi
        """
        if denied := self._require_admin(event):
            return denied
        try:
            conn = await self._get_connection(event)
            response = await self._collect_prompt_response(conn, message)
            return response or "No response from pi."
        except PiError as exc:
            return f"Error: {exc}"

    @filter.llm_tool(name="pi_get_session_info")
    async def pi_get_session_info(self, event: AstrMessageEvent) -> str:
        """Get information about the current pi session."""
        if denied := self._require_admin(event):
            return denied
        try:
            info = await self.pi_connection_manager.get_session_info(event)
            return format_session_info(info)
        except PiError as exc:
            return f"Error: {exc}"

    @filter.llm_tool(name="pi_run_command")
    async def pi_run_command(self, event: AstrMessageEvent, command: str) -> str:
        """Execute a pi slash command in the current session.

        Args:
            command(string): The slash command to execute (without the leading /)
        """
        if denied := self._require_admin(event):
            return denied
        try:
            conn = await self._get_connection(event)
            slash = command if command.startswith("/") else f"/{command}"
            response = await self._collect_prompt_response(conn, slash)
            return self._paginate_legacy_output(event, response)
        except PiError as exc:
            return f"Error: {exc}"

    @filter.llm_tool(name="pi_legacy_output_next")
    async def pi_legacy_output_next(self, event: AstrMessageEvent) -> str:
        """Read the next page of truncated legacy Pi command output."""
        if denied := self._require_admin(event):
            return denied
        return self._next_legacy_output_page(event)

    async def pi_get_available_commands(self, event: AstrMessageEvent) -> str:
        """List the slash commands available in the current pi session."""
        if denied := self._require_admin(event):
            return denied
        try:
            conn = await self._get_connection(event)
            commands = await conn.get_commands()
            return format_commands_list(commands)
        except PiError as exc:
            return f"Error: {exc}"

    @filter.llm_tool(name="pi_abort")
    async def pi_abort(self, event: AstrMessageEvent) -> str:
        """Abort the current pi operation."""
        if denied := self._require_admin(event):
            return denied
        try:
            conn = await self._get_connection(event)
            await conn.abort()
            return "Abort request sent to pi."
        except PiError as exc:
            return f"Error: {exc}"

    @filter.llm_tool(name="pi_reply_ui")
    async def pi_reply_ui(
        self, event: AstrMessageEvent, request_id: int, value: str
    ) -> str:
        """Reply to a pending pi extension UI request.

        Args:
            request_id(number): The local request id shown by pi
            value(string): The reply value (yes/no for confirm, option text or number for select, text for input/editor)
        """
        if denied := self._require_admin(event):
            return denied
        try:
            conn = await self._get_connection(event)
            ui_request = conn.get_ui_request_by_local_id(request_id)
            if ui_request is None:
                return f"No pending request with id {request_id}."

            if ui_request.method == "confirm":
                confirmed = value.strip().lower() in ("yes", "y", "true", "1")
                await conn.confirm_ui_request(ui_request.request_id, confirmed)
                status = "confirmed" if confirmed else "declined"
            elif ui_request.method == "select":
                selected = resolve_select_option(ui_request, value)
                if selected is None:
                    return f"Invalid option '{value}'. Use the option text or a 1-based number."
                await conn.reply_ui_request(ui_request.request_id, selected)
                status = f"selected '{selected}'"
            elif ui_request.method in ("input", "editor"):
                await conn.reply_ui_request(ui_request.request_id, value)
                status = "replied"
            else:
                await conn.reply_ui_request(ui_request.request_id, value)
                status = "replied"

            response = await self._collect_events(conn.read_response())
            return f"{status} request #{request_id}.\n{response}".strip()
        except PiError as exc:
            return f"Error: {exc}"
