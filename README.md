# astrbot_plugin_pi_agent

一个独立维护的 AGPL-3.0 AstrBot 插件，将本地 [Pi](https://github.com/earendil-works/pi) 作为长任务 worker 接入 AstrBot。Pi 作为 AstrBot 的通用 Agent 执行器，适合处理代码、脚本、研究、自动化、文件操作、工具驱动流程以及其他长期、多步骤任务；AstrBot 负责分配、检查、读取和管理。

它解决的是 AstrBot Agent 会话不适合长时间占用的问题：主模型调用 `pi_agent` 后立即拿到 `task_id`，Pi 在独立进程和独立 session 中继续工作；主模型可以在后续回合通过任务工具读取对应的 native session，同时正常处理当前会话的其他消息。

## 重要行为

- **不会等待 Pi 回合结束**：`pi_agent` 只创建任务、保存上下文并排队启动 worker，立即返回结构化结果。
- **不占用当前 AstrBot 工具调用**：Pi 的 JSONL stdin/stdout 由后台 worker 处理，不把长任务包在一次 AstrBot 工具调用里。
- **没有硬超时和空闲超时**：插件不会因为任务运行超过 900 秒、某段时间没有输出，或一次观察调用较慢而杀死 Pi。
- **AstrBot 主动检查**：Pi worker 的 stdout JSONL 只由插件用于维持 RPC/worker 生命周期；`pi_task_status` 不返回 Pi 内容，`pi_task_poll` 才按 AstrBot 的调用显式请求一次短状态检查。
- **Pi 完全静默**：Pi 不向任何 AstrBot 会话发送内容；任务进入 completed、failed、cancelled 或 orphaned 后，插件只通过 AstrBot 公开 StarTools 事件入口将终态通知提交回普通事件队列，由主模型自己读取 Pi session 并决定是否回复或发送文件。终态唤醒禁止转发 Pi 原文、JSONL、工具日志和内部状态，只允许发送整理后的用户可见回复。
- **原生会话直接读取**：`pi_task_read` 默认只读取对应 Pi 原生 session JSONL 最近 50,000 个字符；用户明确要求完整会话时使用 `pi_task_read_full`。插件不构造第二份内容历史，不提炼摘要，不解释错误，AstrBot 自己读取和解析会话。
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
    └── TaskRegistry ◄──┘  任务元数据、session 路径、生命周期
             │
             └── pi_task_poll/status/result（短调用）
```

主要模块：

- `pi_agent_bridge/runtime.py`：固定选择插件内置 Node `22.19.0` / Pi `0.84.2` runtime，不向用户暴露可执行文件路径覆盖配置。
- `pi_agent_bridge/rpc.py`：公开 Pi RPC 的 JSONL 读写、事件游标、steer、cancel、resume。
- `pi_agent_bridge/registry.py`：SQLite WAL 任务状态、session 路径、进程信息、生命周期和 retention。
- `pi_agent_bridge/scheduler.py`：并发限制、后台观察、worker 生命周期和重启接管。
- `pi_agent_bridge/service.py`：给 AstrBot 工具使用的任务控制、50,000 字符 recent-tail 读取和 full-session 读取 facade。
- `pi_agent_bridge/context.py`：构造仅包含主模型整理后任务请求的 Pi 初始 prompt；不读取 AstrBot 人设、历史、事件或媒体上下文。
- `pi_agent_bridge/provider.py`：读取 AstrBot 选定 Provider 的连接地址、鉴权和模型绑定；将插件显式配置的 PiModelSettings 写入任务专属 Pi `models.json`。
- `pi_agent_bridge/artifacts.py`：保留兼容模块；新异步任务不自动扫描或提炼 workspace 内容。
- `pi_agent_bridge/normal_pipeline.py`：使用 AstrBot 公开 StarTools.create_message/create_event 将 Pi 终态通知提交回普通事件 pipeline；不可用时不直接发送 Pi 内容。
- Pi native session 自动压缩：异步和 legacy 新会话都会启用 Pi 官方 `set_auto_compaction`，避免长期上下文无限膨胀。
- Pi 终态唤醒：任务完成、失败、取消或失联时，插件通过 AstrBot 公开事件入口把终态通知重新提交到原会话普通 pipeline；插件不直接发送 Pi 内容。

## 环境要求

- AstrBot 4.x 或更高版本。
- Linux/WSL x64 是首版固定部署目标；runtime adapter 同时处理 Windows/WSL 路径。
- 首选插件随发行版附带的 Node `22.19.0` 与 Pi `0.84.2` runtime。源码 Git 仓库不提交 Node/Pi 二进制；未安装 runtime release asset 时，必须自行安装 Pi CLI 并确保 `pi` 在 AstrBot 进程的 `PATH` 中。
- 选择一个 AstrBot provider 并配置可用密钥。首版只接受 OpenAI-compatible provider。

后台 Pi 使用 `pi_model` 选择的 AstrBot Provider/model 作为模型绑定，但不会自动继承该 Provider 的上下文、推理、输出、模态、采样、成本或兼容字段。Pi 的运行参数全部由以下插件配置项明确控制：`pi_thinking_level`、`pi_context_window`、`pi_max_output_tokens`、`pi_input_modalities`、`pi_temperature`、`pi_top_p`、`pi_top_k`、`pi_min_p` 和 `pi_sampling_params`。填写 0 或留空的数值字段不写入 Pi 配置，由 Pi 使用默认值；Provider 只提供 OpenAI-compatible 连接地址、鉴权和已选模型绑定。

## 安装

```bash
cd /path/to/astrbot/data/plugins
git clone https://github.com/zhyx111999/astrbot_plugin_pi_agent.git astrbot_plugin_pi_agent
```

随后二选一准备运行时：安装本项目对应版本的 runtime release asset，或按照 Pi 官方安装方式安装 Pi CLI 并确认 AstrBot 进程可执行 `pi --version`。重启 AstrBot 或重新加载插件。首次使用异步任务时，插件会在自己的状态目录创建 SQLite WAL registry、native sessions、workspaces 和任务元数据。

## 配置

配置文件为 `_conf_schema.json`，常用项如下：

| 配置项 | 默认值 | 作用 |
| --- | ---: | --- |
| `enable_async_tasks` | `true` | 开启观察式后台任务桥。关闭后只保留旧 `/pi`、`/pic` 线路。 |
| `pi_model` | `""` | 只选择一个 AstrBot 已配置的 Provider/model 绑定；不再从这里读取或覆盖 Pi 的推理、上下文、输出和采样参数。 |
| `pi_thinking_level` | `max` | Pi 官方 thinking level：`off`、`minimal`、`low`、`medium`、`high`、`xhigh` 或 `max`。当前两个 gpt-5.6 模型均使用 `max`。 |
| `pi_context_window` | `0` | Pi 上下文窗口；0 表示不写入，由 Pi 默认值决定。 |
| `pi_max_output_tokens` | `0` | Pi 最大输出 token；0 表示不写入，由 Pi 默认值决定。 |
| `pi_input_modalities` | `["text", "image"]` | Pi 模型输入模态。当前支持 text/image。 |
| `pi_temperature` | `0.5` | Pi temperature。 |
| `pi_top_p` | `1.0` | Pi top-p。 |
| `pi_top_k` | `0` | Pi top-k；0 表示不写入。 |
| `pi_min_p` | `0.0` | Pi min-p；0 表示不写入。 |
| `pi_sampling_params` | `{}` | 额外写入 Pi `samplingParams` 的 JSON 参数。 |
| `pi_session_dir` | `~/.pi/agent/sessions` | 旧版 `/pi`、`/pic` 线路使用的 Pi 原生会话目录。后台任务使用独立会话。 |
| `state_directory` | `~/.pi/astrbot_plugin_pi_agent` | 桥接状态目录，存放任务数据库、会话、Agent 配置、工作区和 artifact 元数据。 |
| `task_database` | `~/.pi/astrbot_plugin_pi_agent/tasks.db` | SQLite WAL 任务注册表路径。 |
| `workspace_root` | `~/.pi/astrbot_plugin_pi_agent/workspaces` | 任务工作区根目录，每个任务使用独立子目录。 |
| `poll_interval_seconds` | `60` | 后台观察周期，不是任务超时。 |
| `session_retention_hours` | `24` | 只清理已完成、失败、取消任务的元数据和 artifact。活动/暂停/orphaned 任务不被误删。 |
| `max_concurrent_tasks` | `4` | 同时运行的独立 Pi worker 数量。 |
| `command_timeout_seconds` | `10` | 仅限制 poll/observer 的 `get_state` 和 steer/cancel/resume 等短 RPC 确认；不是任务硬超时或空闲超时。 |
| `pi_skill_paths` | `[]` | 追加的 Pi Skill 目录；每个路径单独一项，填写包含 `SKILL.md` 的目录绝对路径。 |
| `pi_extension_paths` | `[]` | 追加的 Pi 用户扩展文件或目录；每个路径单独一项，按 Pi 官方 `--extension` 参数加载。 |
| `pi_mcp_config_paths` | `[]` | 外部 Pi 扩展或 MCP 配置路径。当前版本不支持加载，必须保持为空。 |

异步任务的 worker 生命周期错误会使任务转为 `failed`；AstrBot 或插件重载后，未终态任务会从 native session 自动重启并继续，不再因为旧 stdio worker 消失而直接变成 orphaned；Provider 具体错误仍保留在 Pi 原生 session 中，由 AstrBot 通过 `pi_task_read` 自己读取和判断。legacy `pi_open_session` 与 `pi_agent` 使用不同 ID，但 `pi_session_inspect` 和 `pi_session_delete` 现在都可按对应类型处理。legacy 会话创建后即使 Pi 尚未落盘 JSONL，也能在当前插件进程中被列出、检查和删除。

`pi_task_status` 只返回 AstrBot 任务和 native session 元数据，不返回会话内容。`pi_task_poll` 是 AstrBot 主动发起的一次受限 worker 状态检查，并返回最近 8,000 个字符的 native session raw tail，不做摘要、改写、分类、错误提炼或结果判断。普通查看调用 `pi_task_read(task_id)`，返回对应 Pi 原生 JSONL session 最近 50,000 个字符的尾部。用户明确要求完整会话时，使用 `pi_task_read_full(task_id, cursor?, limit?)` 按原生 JSONL 行分页读取；`pi_task_result` 仅作为最近内容兼容别名。

Pi 官方运行时保持不变。插件不会扫描或继承 AstrBot 的 Skill、MCP、工具或扩展资源。配置的每个 Skill 目录会在对应 worker 的启动命令中作为独立的 `--skill <path>` 参数传递。填写方式是：在 `pi_skill_paths` 列表中逐项填写包含 `SKILL.md` 的目录绝对路径。

Pi 用户扩展通过 `pi_extension_paths` 配置，填写扩展文件或扩展目录的绝对路径。任务启动时会作为独立的 `--extension <path>` 参数传给 Pi。Pi 官方内置工具仍然正常可用，用户扩展工具会在 Pi worker 内按 Pi 官方规则注册。

MCP 和 AstrBot 工具不会自动继承。`pi_mcp_config_paths` 必须保持空列表 `[]`；Pi `0.84.2` RPC 没有公开的原生 MCP 入口，填写任意路径会返回结构化 unsupported-capability envelope，路径不会传给 Pi，也不会自动导入 AstrBot MCP 服务。

## AstrBot LLM 工具

### 异步任务工具

| 工具 | 作用 |
| --- | --- |
| `pi_agent(prompt, workspace?)` | 创建新的隔离 Pi 任务，适合通用 Agent 执行代码、脚本、研究、自动化、文件操作、工具驱动流程和其他长期、多步骤工作；立即返回 `task_id`。 |
| `pi_task_status(task_id)` | 读取 AstrBot 任务和 native session 控制元数据，不读取会话内容。 |
| `pi_task_list()` | 列出全部登记的异步 Pi 任务，包含 owner 和 task/session 元数据。 |
| `pi_task_read(task_id)` | 普通查看入口，直接读取对应 Pi 原生 session JSONL 最近 50,000 个字符尾部，不做内容处理。 |
| `pi_task_read_full(task_id, cursor?, limit?)` | 用户明确要求时按原生 session JSONL 行分页读取完整会话。 |
| `pi_task_result(task_id)` | `pi_task_read` 的兼容别名，返回最近会话内容。 |
| `pi_task_poll(task_id)` | 由 AstrBot 主动请求一次短 Pi 状态检查，并返回最近 8,000 字符的 native session raw tail；不做解释。 |
| `pi_task_follow_up(task_id, message)` | owner 或管理员使用 Pi steer 向活动任务追加要求。 |
| `pi_task_resume(task_id)` | owner 或管理员恢复可恢复任务。 |
| `pi_task_cancel(task_id)` | owner 或管理员取消 worker，但保留任务历史。 |
| `pi_task_delete(task_id)` | owner 或管理员取消并删除任务元数据及任务资源。 |
| `pi_session_list()` | 列出全部登记的异步 Pi session；legacy session 仍按原有管理员线路处理。 |
| `pi_session_inspect(session_id)` | 检查 `pi_agent` 的 task ID 或 legacy `pi_open_session` session ID。 |
| `pi_session_resume(task_id)` | 恢复任务关联的 session。仅接受 `pi_agent` task ID。 |
| `pi_session_delete(session_id)` | 删除 task-owned session 或 legacy session。 |
| `pi_legacy_output_next()` | 获取被截断的旧版 Pi 命令输出下一页。 |
| `pi_artifact_inspect(task_id)` | 兼容读取旧任务 artifact 元数据；新任务不自动扫描或登记 workspace 内容。 |

所有异步控制工具都返回 JSON envelope。`status` 只返回控制元数据；`poll` 额外返回有界 native session raw tail；`read/result` 额外返回 native session JSONL 原始行。成功和失败都交给主模型二次加工：

```json
{
  "schema_version": "1",
  "ok": true,
  "operation": "task_poll",
  "task_id": "task-...",
  "status": "running",
  "progress": {},
  "session_lines": [],
  "error": null
}
```

### 任务分工建议

主模型默认应把除极简单纯对话外的任务交给 `pi_agent`。以下情况应优先使用 `pi_agent`，即使任务预计很快完成：

- 任何代码、脚本、测试、调试或文件修改；
- 需要读取或写入文件、媒体或工作区；
- 需要联网研究、资料整理或事实核验；
- 需要调用工具、执行命令或进行结果验证；
- 需要自动化、多代理或独立工作环境；
- 用户要求执行、制作、处理或完成某项具体工作。

只有基础解释、简单翻译、短句改写和不涉及工具/文件的普通闲聊，才留给 AstrBot 自己处理。

调用 `pi_agent` 后必须立即结束当前 AstrBot tool loop，不要在同一回合调用 `pi_task_poll`、`pi_task_status` 或 `pi_task_read`。后续用户回合需要查询时，最多调用一次 list/status/poll/read，工具返回后再次结束当前回合；不要因为状态是 `running` 就在同一回合重复轮询。legacy `pi_open_session` 和 `pi_send_message` 是可能等待 Pi 回复的同步兼容线路，只有用户明确要求 `/pi` 或已有交互 session 时才使用，后台任务必须使用 `pi_agent`。

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

AstrBot 重启时会尝试重新接管仍存活的 worker。标准 stdin/stdout RPC 无法安全接管时，任务会标记为 `orphaned`，不会重复启动第二个写入进程；只有显式 `pi_task_resume` 或 `pi_session_resume` 才会从 session 文件恢复。删除操作会取消任务、删除 registry 元数据，并按任务资源策略清理受插件管理的 native session、工作区和 agent 配置目录。插件不需要为新任务清理复制的事件 snapshot 或自动登记的 artifact。

## 开发与验证

```bash
python -m pytest -q
ruff check main.py pi_agent_bridge pi_legacy tests
python -m compileall -q main.py pi_agent_bridge pi_legacy
git diff --check
```

本项目是独立 fork。请不要把改动推送到上游 Pi 或 AstrBot 仓库；发布时推送到个人 `astrbot_plugin_pi_agent` 仓库。
