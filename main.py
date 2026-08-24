import json
import inspect
import sys
from pathlib import Path
from typing import Any

# Ensure the sibling `pi_legacy` package is importable when AstrBot loads
# this file directly as a standalone module.
sys.path.insert(0, str(Path(__file__).parent))

from pi_legacy import PiConnection, PiConnectionManager, PiError
from pi_legacy.commands import (
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
    PiTaskService,
    TaskScheduler,
    TaskRegistry,
)
from pi_agent_bridge.context import event_owner_key
from pi_agent_bridge.normal_pipeline import enqueue_terminal_wakeup
from pi_agent_bridge.provider import (
    PiModelSettings,
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

def _terminal_wakeup_note(task_id: str, status: str, reason: str) -> str:
    """Build a user-facing reply policy for AstrBot's native wake event."""

    return (
        "后台 Pi Agent 任务已进入终态。"
        f"任务 ID：{task_id}；状态：{status}；原因：{reason}。\n"
        "请先读取该任务的 Pi 原生会话，再根据原用户需求判断是否需要回复。"
        "不要把 Pi 会话原文、JSONL、工具调用、命令输出、内部状态、错误堆栈或系统日志直接发送给用户。"
        "不要复制 Pi 的过程性总结，也不要发送以省略号、系统腔或装饰性颜文字结尾的原始文本。"
        "如确实需要通知用户，只发送一条经过主 Agent 整理的、简洁自然的普通用户回复；"
        "需要发送文件时使用 send_message_to_user 发送文件，并附带简短说明。"
        "如果没有有意义的用户可见结果，则不要发送消息。"
    )


USAGE = """Pi Agent 命令帮助

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


class PiAgentPlugin(Star):
    """Connect AstrBot to a local pi agent for session management, chat, and code tasks."""

    def __init__(self, context: Context, config=None):
        try:
            super().__init__(context, config=config)
        except TypeError:
            # Keep standalone test doubles and older AstrBot Star bases working.
            super().__init__(context)
        self.plugin_config = config if config is not None else {}
        self.astrbot_adapter = AstrBotAdapter(context, self.plugin_config)
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
        logger.info("PiAgent initialized")

    async def initialize(self):
        """Async initialization hook called after the Star is instantiated."""
        if self._config_bool("enable_async_tasks", True):
            await self._ensure_task_service()
        logger.info("PiAgent plugin initialized.")

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
                    "The pi_model setting must select an AstrBot-configured model"
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
            settings = PiModelSettings.from_dict(
                descriptor.get("model_settings", {})
                if isinstance(descriptor.get("model_settings", {}), dict)
                else {}
            )
            binding = build_provider_binding(
                provider_id=source_id,
                provider=provider,
                agent_dir=agent_dir,
                model_settings=settings,
            )
            return PiWorkerConfig(
                provider=binding.pi_provider_id,
                model=binding.model,
                environment=binding.environment,
                agent_dir=binding.agent_dir,
                skill_paths=configured_skill_paths_for_worker(),
                extension_paths=configured_extension_paths_for_worker(),
                thinking_level=settings.thinking_level,
            )

        async def wake_terminal_task(task, reason: str) -> None:
            """Relay a Pi terminal wakeup into AstrBot's normal event pipeline."""

            status = task.status.value
            note = _terminal_wakeup_note(task.task_id, status, reason)
            try:
                await enqueue_terminal_wakeup(
                    context=self.astrbot_adapter.context,
                    session_origin=task.owner_key,
                    message=note,
                )
            except Exception:  # noqa: BLE001
                # Never fall back to direct Pi-content delivery. A host without
                # the public event factory simply leaves the task readable.
                logger.exception(
                    "Failed to relay Pi terminal wakeup into normal pipeline for %s",
                    task.task_id,
                )

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
                terminal_task_callback=wake_terminal_task,
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
            return "Permission denied. Pi Agent is only available to AstrBot administrators."
        return None

    def _require_task_permission(self, event: AstrMessageEvent) -> str | None:
        """Keep async task access owner-scoped; the flag only grants admin-wide access."""
        return None

    def _can_manage_all_tasks(self, event: AstrMessageEvent) -> bool:
        """AstrBot administrators may manage every registered Pi task."""
        return event.is_admin()

    def _task_owner_key(self, event: AstrMessageEvent) -> str:
        return event_owner_key(event)

    def _task_is_visible(self, event: AstrMessageEvent, task) -> bool:
        """Every registered task is readable; writes are owner/admin-only."""
        return True

    def _task_is_manageable(self, event: AstrMessageEvent, task) -> bool:
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
        """Return any registered task for read-only inspection."""

        if self._task_registry is None:
            return None
        try:
            task = self._task_registry.get_task(task_id)
        except Exception:  # noqa: BLE001
            return None
        return task if self._task_is_visible(event, task) else None

    def _manageable_task(self, event: AstrMessageEvent, task_id: str):
        task = self._visible_task(event, task_id)
        return task if task is not None and self._task_is_manageable(event, task) else None

    async def _service_for_task(
        self, event: AstrMessageEvent, task_id: str, *, write: bool = False
    ) -> PiTaskService:
        """Load the bridge before enforcing task visibility.

        Lazy initialization means the registry is unavailable until the first
        tool call. Keeping initialization and authorization together prevents
        disabled-bridge calls from being misreported as missing tasks.
        """

        service = await self._task_service_or_error()
        task = self._manageable_task(event, task_id) if write else self._visible_task(event, task_id)
        if task is None:
            action = "manage" if write else "read"
            raise PermissionError(f"You do not have permission to {action} this Pi task")
        return service

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
        """Use Pi Agent as an independent general-purpose Agent tool.

        Pi Agent is the default executor for any task that is not extremely
        simple and purely conversational. It can independently complete code,
        scripts, research, automation, file operations, testing, media/file
        handling, tool-driven workflows, and other work in an isolated worker.
        Prefer this tool whenever the task involves tools, files, external
        information, execution, validation, or any non-trivial reasoning.
        Give it a complete objective and constraints; it executes the work
        instead of merely explaining how to do it.

        This tool returns a task_id immediately. After it returns, end the
        current AstrBot tool loop. Do not inspect the task in the same turn.
        Only very simple one-turn conversation, such as a basic explanation,
        translation, short rewrite, or casual reply with no tool/file work,
        should stay in AstrBot's own Agent.

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
                    "请先在 pi_model 中选择 AstrBot 已配置的模型",
                )
            service = await self._task_service_or_error()
            owner_key = event_owner_key(event)
            mcp_paths = self._config_value("pi_mcp_config_paths", [])
            if mcp_paths:
                return self._bridge_error(
                    operation,
                    "Pi MCP integration is unsupported by the bundled Pi RPC bridge; "
                    "configure a separately maintained Pi extension first",
                )
            descriptor = {
                "source_provider_id": provider_id,
                "model_settings": PiModelSettings.from_config(
                    self.plugin_config
                ).as_dict(),
            }

            # Only the main model's already-refined tool argument is sent to
            # Pi. The descriptor is durable control metadata and is filtered
            # from the worker prompt by the underscore-prefixed key.
            result = await service.create_task(
                owner_key=owner_key,
                task=prompt,
                context={WORKER_DESCRIPTOR_KEY: dict(descriptor)},
                workspace=workspace or None,
            )
            return self._bridge_dump(result)
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc))

    @filter.llm_tool(name="pi_task_status")
    async def pi_task_status(self, event: AstrMessageEvent, task_id: str) -> str:
        """Inspect metadata for a task previously created by pi_agent.

        This is a supporting read-only tool, not an Agent execution tool.
        After it returns, end the current tool loop; do not chain another
        Pi inspection call in the same turn.

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
        """Find tasks previously created by pi_agent.

        This is a supporting directory tool used to select an existing Agent
        task. It does not execute work. After it returns, end the current tool
        loop; do not chain list -> poll -> read in one turn.
        """
        operation = "task_list"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied)
        try:
            service = await self._task_service_or_error()
            return self._bridge_dump(service.list_tasks())
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc))

    @filter.llm_tool(name="pi_task_read")
    async def pi_task_read(
        self, event: AstrMessageEvent, task_id: str
    ) -> str:
        """Read the recent context of a pi_agent task for AstrBot.

        This supporting inspection tool returns at most 50,000 characters
        from the native session tail without summarizing or rewriting it.
        Use pi_task_read_full only when the user explicitly asks for the
        complete session, then end the current tool loop.

        After this tool returns, end the current AstrBot tool loop. Do not
        immediately call poll, status, result, or read again in the same turn.

        Args:
            task_id(string): Task id returned by pi_agent
        """
        operation = "task_read"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id)
            return self._bridge_dump(service.read(task_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_task_read_full")
    async def pi_task_read_full(
        self, event: AstrMessageEvent, task_id: str, cursor: int = 0, limit: int = 100
    ) -> str:
        """Read complete native Pi session JSONL lines when explicitly requested.

        Normal inspection should use pi_task_read, which returns only the
        recent 50,000-character tail. Use this tool only when the user asks
        to inspect the complete session. End the current tool loop after it
        returns and continue with a later page only when necessary.

        Args:
            task_id(string): Task id returned by pi_agent
            cursor(number): Zero-based native session line cursor
            limit(number): Maximum complete session lines to return
        """
        operation = "task_read_full"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id)
            return self._bridge_dump(
                service.read_full(task_id, cursor=cursor, limit=limit)
            )
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_task_result")
    async def pi_task_result(
        self, event: AstrMessageEvent, task_id: str, offset: int = 0, limit: int = 100
    ) -> str:
        """Compatibility reader for a task previously created by pi_agent.

        This is not an Agent execution tool or a summarized result endpoint.
        Use pi_task_read for normal inspection and end the current tool loop.

        After this tool returns, end the current AstrBot tool loop. Do not
        chain another Pi inspection call in the same turn.

        Args:
            task_id(string): Task id returned by pi_agent
            offset(number): Zero-based session line cursor to continue from
            limit(number): Maximum native session lines to return
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
        """Ask Pi for one short state observation without returning Pi content.

        AstrBot explicitly triggers this read-only check. It does not inject
        the current caller's context into the existing Pi session.

        Perform at most one poll in the current turn. After it returns, end
        the current AstrBot tool loop and wait for a later turn before polling
        again, even when the status is still running.

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
        """Send an explicit follow-up requirement to the existing Pi session.

        This is a write operation for the task owner or an AstrBot
        administrator. It changes the selected task only; it does not create
        a new session or inject the caller's full AstrBot context.

        Args:
            task_id(string): Task id returned by pi_agent
            message(string): Additional instruction to steer the worker
        """
        operation = "task_follow_up"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id, write=True)
            return self._bridge_dump(await service.follow_up(task_id, message))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_task_resume")
    async def pi_task_resume(self, event: AstrMessageEvent, task_id: str) -> str:
        """Resume an existing Pi task/session without rebuilding its context.

        The original task keeps its provider, model, persona, conversation
        snapshot, and event history. Only the owner or an administrator may
        perform this write operation.

        Args:
            task_id(string): Task id returned by pi_agent
        """
        operation = "task_resume"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id, write=True)
            return self._bridge_dump(await service.resume(task_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_task_cancel")
    async def pi_task_cancel(self, event: AstrMessageEvent, task_id: str) -> str:
        """Cancel an existing Pi task without deleting its durable history.

        This is a write operation for the task owner or an AstrBot
        administrator.

        Args:
            task_id(string): Task id returned by pi_agent
        """
        operation = "task_cancel"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id, write=True)
            return self._bridge_dump(await service.cancel(task_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_task_delete")
    async def pi_task_delete(self, event: AstrMessageEvent, task_id: str) -> str:
        """Delete an existing Pi task and its managed resources.

        This is a write operation for the task owner or an AstrBot
        administrator. Read-only inspection does not permit deletion.

        Args:
            task_id(string): Task id returned by pi_agent
        """
        operation = "task_delete"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id, write=True)
            return self._bridge_dump(await service.delete(task_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_session_list")
    async def pi_session_list(self, event: AstrMessageEvent) -> str:
        """List every registered async Pi session for AstrBot to inspect.

        This is a read-only directory operation. It does not create or
        reconfigure any Pi session.
        """
        operation = "session_list"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied)
        try:
            service = await self._task_service_or_error()
            owner = None
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
        """Inspect an async task session, or an administrator-only legacy session.

        Inspection is read-only and never injects the current caller's
        context into the inspected session.

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
        """Resume an existing async Pi session without rebuilding its context.

        This is a write operation for the task owner or an AstrBot
        administrator.

        Args:
            task_id(string): Task id returned by pi_agent
        """
        operation = "session_resume"
        if denied := self._require_task_permission(event):
            return self._bridge_error(operation, denied, task_id=task_id)
        try:
            service = await self._service_for_task(event, task_id, write=True)
            return self._bridge_dump(await service.session_resume(task_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=task_id)

    @filter.llm_tool(name="pi_session_delete")
    async def pi_session_delete(self, event: AstrMessageEvent, session_id: str) -> str:
        """Delete an async Pi session or an administrator-only legacy session.

        Async task deletion requires task-owner or administrator permission.
        Inspection and reading never grant deletion permission.

        Args:
            session_id(string): Task ID from pi_agent or session ID from pi_open_session
        """
        operation = "session_delete"
        task = self._visible_task(event, session_id)
        if task is not None:
            try:
                service = await self._service_for_task(event, session_id, write=True)
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
        """Inspect artifacts produced by an async Pi task.

        This is read-only and does not modify or reconfigure the Pi session.

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
