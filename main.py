import asyncio
import hashlib
import json
import inspect
import sys
from pathlib import Path
from typing import Any

# AstrBot loads main.py directly; make sibling bridge packages importable.
sys.path.insert(0, str(Path(__file__).parent))

from pi_agent_bridge import (
    PiTaskService,
    TaskScheduler,
    TaskRegistry,
)
from pi_agent_bridge.context import event_owner_key
from pi_agent_bridge.normal_pipeline import (
    enqueue_progress_wakeup,
    enqueue_terminal_wakeup,
)
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
from astrbot.api.star import Context, Star


class _PiTaskWakeFilter:
    """Activate only plugin-generated task update events."""

    def __init__(self, _raise_error: bool = True) -> None:
        pass

    def filter(self, event: AstrMessageEvent, _config: Any) -> bool:
        raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return (
            isinstance(raw_message, dict)
            and raw_message.get("origin") == "astrbot_plugin_pi_agent"
            and raw_message.get("kind") in {"progress_wakeup", "terminal_wakeup"}
        )


def _terminal_wakeup_note(
    task_id: str,
    status: str,
    reason: str,
    tail: str,
) -> str:
    """Build a terminal prompt with a bounded native-session tail."""

    return (
        "后台 Pi Agent 任务已进入终态。"
        f"任务 ID：{task_id}；状态：{status}；原因：{reason}。\n"
        "下面附带对应 Pi 原生会话最近 8,000 个字符。"
        "请根据这些内容和原用户需求整理一条简洁自然的最终回复。"
        "不要直接发送 Pi 会话原文、JSONL、工具调用、命令输出、内部状态、错误堆栈或系统日志。"
        "不要复制 Pi 的过程性总结，也不要发送以省略号、系统腔或装饰性颜文字结尾的原始文本。"
        "需要发送文件时使用 send_message_to_user 发送文件，并附带简短说明。"
        "如果没有有意义的用户可见结果，则不要发送消息。\n"
        "--- Pi 最近会话尾部开始 ---\n"
        f"{tail}\n"
        "--- Pi 最近会话尾部结束 ---"
    )


def _progress_wakeup_note(task_id: str, tail: str) -> str:
    """Build an intermediate progress prompt with a bounded native tail."""

    return (
        "后台 Pi Agent 任务仍在运行，这是一次中间进度更新。"
        f"任务 ID：{task_id}。\n"
        "请根据下面最近的 Pi 原生会话尾部，整理一条简短、自然、面向用户的当前进展回复。"
        "不要直接复制 JSONL、工具调用、命令输出、内部状态、错误堆栈或系统日志。"
        "不要调用 send_message_to_user 发送普通文本；本次事件的普通文本回复会由正常响应管线发送。"
        "如果尾部只有重复的内部过程或没有有意义的进展，可以不发送消息。\n"
        "--- Pi 最近会话尾部开始 ---\n"
        f"{tail}\n"
        "--- Pi 最近会话尾部结束 ---"
    )


class PiAgentPlugin(Star):
    """Run isolated Pi Agent tasks from AstrBot's main model."""

    def __init__(self, context: Context, config=None):
        super().__init__(context, config=config)
        self.plugin_config = config if config is not None else {}
        self.context = context
        self.pi_task_service: PiTaskService | None = None
        self.pi_task_scheduler: TaskScheduler | None = None
        self._task_registry: TaskRegistry | None = None
        self._task_service_lock = None
        self._pending_terminal_wakeups: set[asyncio.Task[None]] = set()
        self._progress_digests: dict[str, str] = {}
        logger.info("PiAgent initialized")

    async def initialize(self):
        """Async initialization hook called after the Star is instantiated."""
        if self._config_bool("enable_async_tasks", True):
            await self._ensure_task_service()
        logger.info("PiAgent plugin initialized.")

    async def terminate(self):
        """Terminate all managed pi connections when the plugin is unloaded."""
        for pending in tuple(self._pending_terminal_wakeups):
            pending.cancel()
        if self._pending_terminal_wakeups:
            await asyncio.gather(
                *self._pending_terminal_wakeups,
                return_exceptions=True,
            )
            self._pending_terminal_wakeups.clear()
        self._progress_digests.clear()
        if self.pi_task_service is not None:
            await self.pi_task_service.shutdown()
            self.pi_task_service = None
        if self.pi_task_scheduler is not None:
            # Stop task-owned children cleanly. Startup recovery resumes each
            # nonterminal native session, avoiding a stale stdio transport.
            await self.pi_task_scheduler.shutdown()
            self.pi_task_scheduler = None
        if self._task_registry is not None:
            self._task_registry.close()
            self._task_registry = None

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
            getter = getattr(self.context, "get_provider_by_id", None)
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

        async def progress_task(task) -> None:
            """Relay one changed native-session tail as an intermediate update."""

            service = self.pi_task_service
            if service is None:
                return
            try:
                tail = service.recent_session_tail(task.task_id, max_chars=8_000)
                if not tail.strip():
                    return
                digest = hashlib.sha256(tail.encode("utf-8")).hexdigest()
                if self._progress_digests.get(task.task_id) == digest:
                    return
                self._progress_digests[task.task_id] = digest
                await enqueue_progress_wakeup(
                    context=self.context,
                    session_origin=task.owner_key,
                    message=_progress_wakeup_note(task.task_id, tail),
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to relay Pi intermediate progress for %s",
                    task.task_id,
                )

        def terminal_note(task, reason: str) -> str:
            service = self.pi_task_service
            tail = ""
            if service is not None:
                try:
                    tail = service.recent_session_tail(task.task_id, max_chars=8_000)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to read terminal Pi session tail for %s",
                        task.task_id,
                    )
            return _terminal_wakeup_note(
                task.task_id,
                task.status.value,
                reason,
                tail,
            )

        async def retry_terminal_wakeup(task, reason: str) -> None:
            """Retry a startup wakeup after AstrBot has loaded its platforms."""

            for delay in (2.0, 4.0, 8.0, 16.0, 30.0):
                await asyncio.sleep(delay)
                try:
                    await enqueue_terminal_wakeup(
                        context=self.context,
                        session_origin=task.owner_key,
                        message=terminal_note(task, reason),
                    )
                    logger.info(
                        "Relayed deferred Pi terminal wakeup for %s",
                        task.task_id,
                    )
                    return
                except ValueError as exc:
                    if not str(exc).startswith("Platform not found:"):
                        logger.exception(
                            "Failed to relay deferred Pi terminal wakeup for %s",
                            task.task_id,
                        )
                        return
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to relay deferred Pi terminal wakeup for %s",
                        task.task_id,
                    )
                    return

            logger.error(
                "Giving up deferred Pi terminal wakeup for %s after platform retries",
                task.task_id,
            )

        async def wake_terminal_task(task, reason: str) -> None:
            """Relay a Pi terminal wakeup into AstrBot's normal event pipeline."""

            try:
                await enqueue_terminal_wakeup(
                    context=self.context,
                    session_origin=task.owner_key,
                    message=terminal_note(task, reason),
                )
            except ValueError as exc:
                if not str(exc).startswith("Platform not found:"):
                    logger.exception(
                        "Failed to relay Pi terminal wakeup into normal pipeline for %s",
                        task.task_id,
                    )
                    return
                retry = asyncio.create_task(
                    retry_terminal_wakeup(task, reason),
                    name=f"pi-terminal-wakeup-{task.task_id}",
                )
                self._pending_terminal_wakeups.add(retry)
                retry.add_done_callback(self._pending_terminal_wakeups.discard)
                logger.warning(
                    "Pi platform is not loaded yet; deferred terminal wakeup for %s",
                    task.task_id,
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
                    self._config_value("poll_interval_seconds", 180)
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
                observation_task_callback=progress_task,
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

        from astrbot.api.star import StarTools

        return Path(StarTools.get_data_dir("astrbot_plugin_pi_agent"))

    async def _task_service_or_error(self) -> PiTaskService:
        if not self._config_bool("enable_async_tasks", True):
            raise RuntimeError("Pi task bridge is disabled in plugin configuration")
        return await self._ensure_task_service()

    # ------------------------------------------------------------------
    # Permission check
    # ------------------------------------------------------------------

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
    # Synthetic task-event activation
    # ------------------------------------------------------------------

    @filter.custom_filter(_PiTaskWakeFilter, priority=100_000)
    async def activate_task_wakeup(self, event: AstrMessageEvent) -> None:
        """Let task wakeups reach the main Agent without using chat prefixes."""

        event.is_wake = True
        event.is_at_or_wake_command = True

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

    @filter.llm_tool(name="pi_task_poll")
    async def pi_task_poll(self, event: AstrMessageEvent, task_id: str) -> str:
        """Observe Pi state and return a bounded raw native-session tail.

        The tail is returned unchanged and is not summarized or interpreted.
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
            return self._bridge_dump(service.session_list(owner_key=None))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc))

    @filter.llm_tool(name="pi_session_inspect")
    async def pi_session_inspect(self, event: AstrMessageEvent, session_id: str) -> str:
        """Inspect an async Pi task session by task ID.

        Args:
            session_id(string): Task ID returned by pi_agent
        """
        operation = "session_inspect"
        try:
            service = await self._service_for_task(event, session_id)
            return self._bridge_dump(service.session_inspect(session_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=session_id)

    @filter.llm_tool(name="pi_session_search")
    async def pi_session_search(
        self,
        event: AstrMessageEvent,
        session_id: str,
        keyword: str,
    ) -> str:
        """Search a Pi native session and return up to 8,000 surrounding chars.

        Args:
            session_id(string): Task ID returned by pi_agent
            keyword(string): Literal keyword to search in the native session
        """
        operation = "session_search"
        try:
            service = await self._service_for_task(event, session_id)
            return self._bridge_dump(service.search_session(session_id, keyword))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(
                operation,
                safe_error_summary(exc),
                task_id=session_id,
            )

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
        """Delete an async Pi task session by task ID.

        Args:
            session_id(string): Task ID returned by pi_agent
        """
        operation = "session_delete"
        try:
            service = await self._service_for_task(event, session_id, write=True)
            return self._bridge_dump(await service.session_delete(session_id))
        except Exception as exc:  # noqa: BLE001
            return self._bridge_error(operation, safe_error_summary(exc), task_id=session_id)

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
