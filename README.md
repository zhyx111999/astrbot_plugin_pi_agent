# astrbot_plugin_pi_agent

一个独立维护的 AGPL-3.0 AstrBot 插件，将本地 [Pi](https://github.com/earendil-works/pi) 作为长任务 worker 接入 AstrBot。

它解决的是 AstrBot Agent 会话不适合长时间占用的问题：主模型调用 `pi_agent` 后立即拿到 `task_id`，Pi 在独立进程和独立 session 中继续工作；主模型可以在后续回合通过短调用读取最新快照，同时正常处理当前会话的其他消息。

## 重要行为

- **不会等待 Pi 回合结束**：`pi_agent` 只创建任务、保存上下文并排队启动 worker，立即返回结构化结果。
- **不占用当前 AstrBot 工具调用**：Pi 的 JSONL stdin/stdout 由后台 worker 处理，不把长任务包在一次 AstrBot 工具调用里。
- **没有硬超时和空闲超时**：插件不会因为任务运行超过 900 秒、某段时间没有输出，或一次观察调用较慢而杀死 Pi。
- **AstrBot 主动检查**：Pi worker 的 stdout JSONL 只由插件内部接收并持久化；`pi_task_status` 不返回 Pi 内容，`pi_task_poll` 才按 AstrBot 的调用显式请求一次短状态检查。
- **Pi 完全静默**：没有完成、失败、进度或无输出通知，后台 observer 不向任何 AstrBot 会话发送消息，也不唤醒主模型。
- **原始会话按游标读取**：`pi_task_read` 按 Pi event cursor 返回原始事件页；插件不把事件提炼成摘要或自然语言结果。历史观察在任务 retention 期间保留。
- **读写权限分离**：普通用户可以列出、检查、轮询和读取全部登记任务；只有任务 owner 或 AstrBot 管理员可以 follow-up、resume、cancel、delete。管理员始终可以管理全部任务。
- **任务彼此隔离**：每个任务有独立进程、Pi session、工作区、事件游标和 registry 记录，同一 AstrBot 会话可同时运行多个 Pi 任务。

Pi 官方 CLI、RPC JSONL 协议和 AstrBot 官方代码均保持原样。本仓库只维护独立适配器和任务桥接层；停用异步桥接后，旧 `/pi`、`/pic` 会话线路仍可用。

## 架构

```text
AstrBot 主模型
    │  pi_agent（短调用，立即返回 task_id）
    ▼
PiTaskService / ToolRegistry
    ▼
TaskScheduler ── PiRpcAdapter ── Pi worker（一个任务一个进程/session）
    │                   │
    └── TaskRegistry ◄──┘  JSONL 事件、游标、快照、artifact
             │
             └── pi_task_poll/status/result（短调用）
```

主要模块：

- `pi_agent_bridge/runtime.py`：固定选择插件内置 Node `22.19.0` / Pi `0.84.2` runtime，不向用户暴露可执行文件路径覆盖配置。
- `pi_agent_bridge/rpc.py`：公开 Pi RPC 的 JSONL 读写、事件游标、steer、cancel、resume。
- `pi_agent_bridge/registry.py`：SQLite WAL 任务状态、快照、游标、artifact 和 retention。
- `pi_agent_bridge/scheduler.py`：并发限制、后台观察、worker 生命周期和重启接管。
- `pi_agent_bridge/service.py`：给 AstrBot 工具使用的任务控制和原始事件读取 facade。
- `pi_agent_bridge/context.py`：创建任务时构造人设、当前事件公开字段和原始消息的一次性快照。
- `pi_agent_bridge/provider.py`：将 AstrBot OpenAI-compatible provider 映射为 Pi 的 worker 配置；密钥只进入子进程环境。
- `pi_agent_bridge/artifacts.py`：文本、Markdown、JSON、文件和媒体 artifact 的统一描述。
- `pi_agent_bridge/wakeup.py`：为将来可用的主模型唤醒入口保留适配边界；没有公开唤醒 API 时只持久化快照。

## 环境要求

- AstrBot 4.x 或更高版本。
- Linux/WSL x64 是首版固定部署目标；runtime adapter 同时处理 Windows/WSL 路径。
- 首选插件随发行版附带的 Node `22.19.0` 与 Pi `0.84.2` runtime。源码 Git 仓库不提交 Node/Pi 二进制；未安装 runtime release asset 时，必须自行安装 Pi CLI 并确保 `pi` 在 AstrBot 进程的 `PATH` 中。
- 选择一个 AstrBot provider 并配置可用密钥。首版只接受 OpenAI-compatible provider。

后台 Pi 会读取所选 AstrBot Provider 的模型配置，并映射到任务专属的 Pi `models.json`：模型能力模态、推理标记、上下文上限、输出上限、自定义请求体、成本和兼容参数都会尽量保持一致。AstrBot 未配置的字段不会被插件硬编码覆盖，而是交给 Pi 的默认值；API key、鉴权头等敏感值只通过任务工作进程环境传递，不写入任务数据库或快照。

## 安装

```bash
cd /path/to/astrbot/data/plugins
git clone https://github.com/zhyx111999/astrbot_plugin_pi_agent.git astrbot_plugin_pi_agent
```

随后二选一准备运行时：安装本项目对应版本的 runtime release asset，或按照 Pi 官方安装方式安装 Pi CLI 并确认 AstrBot 进程可执行 `pi --version`。重启 AstrBot 或重新加载插件。首次使用异步任务时，插件会在自己的状态目录创建 SQLite WAL registry、sessions、workspaces 和 artifact 元数据。

## 配置

配置文件为 `_conf_schema.json`，常用项如下：

| 配置项 | 默认值 | 作用 |
| --- | ---: | --- |
| `enable_async_tasks` | `true` | 开启观察式后台任务桥。关闭后只保留旧 `/pi`、`/pic` 线路。 |
| `pi_model` | `""` | 直接选择 AstrBot 中已经配置好的具体 Provider/模型。所有后台 Pi 任务固定使用这个模型，不继承当前聊天模型。仅支持 OpenAI-compatible Provider。 |
| `pi_session_dir` | `~/.pi/agent/sessions` | 旧版 `/pi`、`/pic` 线路使用的 Pi 原生会话目录。后台任务使用独立会话。 |
| `state_directory` | `~/.pi/astrbot_plugin_pi_agent` | 桥接状态目录，存放任务数据库、会话、Agent 配置、工作区和 artifact 元数据。 |
| `task_database` | `~/.pi/astrbot_plugin_pi_agent/tasks.db` | SQLite WAL 任务注册表路径。 |
| `workspace_root` | `~/.pi/astrbot_plugin_pi_agent/workspaces` | 任务工作区根目录，每个任务使用独立子目录。 |
| `poll_interval_seconds` | `60` | 后台观察周期，不是任务超时。 |
| `session_retention_hours` | `24` | 只清理已完成、失败、取消任务的元数据和 artifact。活动/暂停/orphaned 任务不被误删。 |
| `max_concurrent_tasks` | `4` | 同时运行的独立 Pi worker 数量。 |
| `command_timeout_seconds` | `10` | 仅限制 poll/observer 的 `get_state` 和 steer/cancel/resume 等短 RPC 确认；不是任务硬超时或空闲超时。 |
| `inherit_persona` | `true` | 创建时复制主 Agent 人设；任务 prompt 同时保存当前事件的公开字段和原始消息快照。之后不会自动同步新消息。 |
| `pi_skill_paths` | `[]` | 追加的 Pi Skill 目录；每个路径单独一项，填写包含 `SKILL.md` 的目录绝对路径。 |
| `pi_extension_paths` | `[]` | 追加的 Pi 用户扩展文件或目录；每个路径单独一项，按 Pi 官方 `--extension` 参数加载。 |
| `pi_mcp_config_paths` | `[]` | 外部 Pi 扩展或 MCP 配置路径。当前版本不支持加载，必须保持为空。 |

异步任务的 Pi Provider/RPC 错误会使任务转为 `failed`，并可通过 `pi_task_result` 查看错误内容；不会继续显示为 `running`。legacy `pi_open_session` 与 `pi_agent` 使用不同 ID，但 `pi_session_inspect` 和 `pi_session_delete` 现在都可按对应类型处理。legacy 会话创建后即使 Pi 尚未落盘 JSONL，也能在当前插件进程中被列出、检查和删除。

`pi_task_status` 只返回 AstrBot 任务元数据、Pi session 标识、event cursor 和 observer 时间，不返回 Pi 中间内容。`pi_task_poll` 是 AstrBot 主动发起的一次受限状态检查，返回同样的控制元数据，也不返回 Pi 事件。需要阅读内容时调用 `pi_task_read(task_id, cursor?, limit?)`，它按 cursor 返回原始 Pi 事件，不做摘要、改写或语义筛选；`pi_task_result` 仅作为兼容别名。任务 owner 可以管理自己的任务，普通用户对其他用户任务只有读取权限，AstrBot 管理员可以管理全部任务。后台 observer 只负责维持 worker 和持久化原始事件，永不主动通知聊天。

Pi 官方运行时保持不变。插件不会扫描或继承 AstrBot 的 Skill、MCP、工具或扩展资源。配置的每个 Skill 目录会在对应 worker 的启动命令中作为独立的 `--skill <path>` 参数传递。填写方式是：在 `pi_skill_paths` 列表中逐项填写包含 `SKILL.md` 的目录绝对路径。

Pi 用户扩展通过 `pi_extension_paths` 配置，填写扩展文件或扩展目录的绝对路径。任务启动时会作为独立的 `--extension <path>` 参数传给 Pi。Pi 官方内置工具仍然正常可用，用户扩展工具会在 Pi worker 内按 Pi 官方规则注册。

MCP 和 AstrBot 工具不会自动继承。`pi_mcp_config_paths` 必须保持空列表 `[]`；Pi `0.84.2` RPC 没有公开的原生 MCP 入口，填写任意路径会返回结构化 unsupported-capability envelope，路径不会传给 Pi，也不会自动导入 AstrBot MCP 服务。

## AstrBot LLM 工具

### 异步任务工具

| 工具 | 作用 |
| --- | --- |
| `pi_agent(prompt, workspace?)` | 创建独立长任务，立即返回 `task_id`。 |
| `pi_task_status(task_id)` | 读取持久化 AstrBot 任务控制元数据，不返回 Pi 事件。 |
| `pi_task_list()` | 列出全部登记的异步 Pi 任务，包含 owner 和 task/session 元数据。 |
| `pi_task_read(task_id, cursor?, limit?)` | 只读指定任务的原始 Pi 事件页；按 cursor 继续读取。 |
| `pi_task_result(task_id, offset?, limit?)` | `pi_task_read` 的兼容别名，offset 表示 Pi event cursor。 |
| `pi_task_poll(task_id)` | 由 AstrBot 主动请求一次短 Pi 状态检查；只返回控制元数据，不返回 Pi 内容。 |
| `pi_task_follow_up(task_id, message)` | owner 或管理员使用 Pi steer 向活动任务追加要求。 |
| `pi_task_resume(task_id)` | owner 或管理员恢复可恢复任务。 |
| `pi_task_cancel(task_id)` | owner 或管理员取消 worker，但保留任务历史。 |
| `pi_task_delete(task_id)` | owner 或管理员取消并删除任务元数据及任务资源。 |
| `pi_session_list()` | 列出全部登记的异步 Pi session；legacy session 仍按原有管理员线路处理。 |
| `pi_session_inspect(session_id)` | 检查 `pi_agent` 的 task ID 或 legacy `pi_open_session` session ID。 |
| `pi_session_resume(task_id)` | 恢复任务关联的 session。仅接受 `pi_agent` task ID。 |
| `pi_session_delete(session_id)` | 删除 task-owned session 或 legacy session。 |
| `pi_legacy_output_next()` | 获取被截断的旧版 Pi 命令输出下一页。 |
| `pi_artifact_inspect(task_id)` | 查看任务生成的文本、JSON、文件和媒体 artifact。 |

所有异步控制工具都返回 JSON envelope。`status/poll` 的 envelope 不包含 Pi 原始内容；`read/result` 额外返回 `events` 分页。成功和失败都交给主模型二次加工：

```json
{
  "schema_version": "1",
  "ok": true,
  "operation": "task_poll",
  "task_id": "task-...",
  "status": "running",
  "has_new_meaningful_event": false,
  "progress": {},
  "content": [],
  "artifacts": [],
  "error": null
}
```

### 任务分工建议

主模型应把普通问答、简单改写和一次短工具调用留在 AstrBot 自己的 Agent。以下情况适合使用 `pi_agent`：

- 需要多步研究、编码、测试或文件修改；
- 预计超过一次工具调用，或需要独立工作区；
- 需要并行的独立子任务；
- 用户明确要求后台持续执行。

调用后不要在同一次回合等待 Pi 完成。后续由 AstrBot 根据用户需求调用 `pi_task_list` 找到目标 task，再调用 `pi_task_status` 或 `pi_task_poll` 检查控制状态；发现 event cursor 增长后，调用 `pi_task_read` 读取对应 Pi 原始事件并自行判断下一步。Pi 的完成、失败和中间输出都不会主动进入任何聊天会话。普通用户可读取其他用户任务，但只有 owner 或管理员可以改变任务。

### 旧会话工具与命令

旧的同步会话工具和命令仍保留，用于兼容已有工作流：`pi_open_session`、`pi_list_sessions`、`pi_resume_session`、`pi_send_message`、`pi_get_session_info`、`pi_run_command`、`pi_get_available_commands`、`pi_abort`、`pi_reply_ui`，以及 `/pi`、`/pic` 命令。它们与新的 `pi_agent` 任务模型是两条独立线路。

## 旧命令示例

```text
/pi open /home/user/project
/pi sessions
/pi resume
/pi refactor the auth module
/pic help
```

## 恢复、保留和删除

AstrBot 重启时会尝试重新接管仍存活的 worker。标准 stdin/stdout RPC 无法安全接管时，任务会标记为 `orphaned`，不会重复启动第二个写入进程；只有显式 `pi_task_resume` 或 `pi_session_resume` 才会从 session 文件恢复。删除操作会取消任务、删除 registry 元数据，并按任务资源策略清理受插件管理的 session、工作区、agent 配置目录和 artifact 元数据。

## 开发与验证

```bash
python -m pytest -q
ruff check main.py pi_agent_bridge pi_connector tests
python -m compileall -q main.py pi_agent_bridge pi_connector
git diff --check
```

本项目是独立 fork。请不要把改动推送到上游 Pi 或 AstrBot 仓库；发布时推送到个人 `astrbot_plugin_pi_agent` 仓库。
