# astrbot_plugin_pi_agent

一个由 **Yezi 和 Cz** 维护的开源 AstrBot 插件，将本地 [Pi](https://github.com/earendil-works/pi) 作为长任务 worker 接入 AstrBot。Pi 作为 AstrBot 的通用 Agent 执行器，适合处理代码、脚本、研究、自动化、文件操作、工具驱动流程以及其他长期、多步骤任务；AstrBot 负责分配、检查、读取和管理。

它解决的是 AstrBot Agent 会话不适合长时间占用的问题：主模型调用 `pi_agent` 后立即拿到 `task_id`，Pi 在独立进程和独立 session 中继续工作；主模型可以在后续回合通过任务工具读取对应的 native session，同时正常处理当前会话的其他消息。

## 使用前提

本插件依赖 AstrBot 内置 Agent 能力，不会在关闭 Agent / 工具调用时工作。安装和使用前必须满足：

1. 在 WebUI `配置` -> `Agent 执行方式` 中启用 AI 对话，执行器选择 **内置 Agent**。不要改成 Dify、Coze、百炼或 DeerFlow。
2. 保持函数工具 / 工具调用开启，不要关闭全部 LLM 工具。本插件通过 `pi_agent` 等 LLM 工具把任务交给后台 Pi。
3. 当前聊天使用的模型必须支持 function calling / 工具调用。
4. 当前声明并验证过的消息平台是 `aiocqhttp`（OneBot v11 / QQ）。
5. AstrBot 版本必须满足 `>=4.27.1,<5`。

未打开 AstrBot 内置 Agent 和工具调用时，插件可以加载，但主模型无法创建、检查或回传 Pi 任务。

## 重要行为

- **不会等待 Pi 回合结束**：`pi_agent` 只创建任务、保存上下文并排队启动 worker，立即返回结构化结果。
- **不占用当前 AstrBot 工具调用**：Pi 的 JSONL stdin/stdout 由后台 worker 处理，不把长任务包在一次 AstrBot 工具调用里。
- **没有硬超时和空闲超时**：插件不会因为任务运行超过 900 秒、某段时间没有输出，或一次观察调用较慢而杀死 Pi。
- **AstrBot 主动检查**：Pi worker 的 stdout JSONL 只由插件用于维持 RPC/worker 生命周期；`pi_task_status` 不返回 Pi 内容，`pi_task_poll` 才按 AstrBot 的调用显式请求一次短状态检查。
- **Pi 进程完全静默**：Pi 不向任何 AstrBot 会话直接发送内容；后台检活默认每 180 秒、按配置可调，插件只读取 Pi 原生会话最近 8,000 个字符并交给主 Agent 整理中间进度。任务进入 completed、failed、cancelled 或 orphaned 后，插件通过 AstrBot 公开 StarTools 事件入口提交终态通知，由主模型自己读取或使用提供的会话内容并决定回复或发送文件。任何中间态和终态都禁止直接转发 Pi 原文、JSONL、工具日志和内部状态。
- **原生会话有界读取**：任务检活、中间态和终态都只使用对应 Pi 原生 session JSONL 最近 8,000 个字符。`pi_session_search` 可以按关键词返回匹配点上下文，总长度最多 8,000 个字符。插件不构造第二份内容历史，不提炼摘要，不解释错误，AstrBot 自己加工会话内容。
- **权限边界分离**：任务所有者按 `平台 + 发送者 ID` 记录，群聊成员不再共享所有权；任务中间态和终态按独立保存的创建会话回传。同一用户可以跨私聊和群聊管理自己的任务，其他用户只能读取，管理员可以管理全部任务。
- **任务彼此隔离**：每个任务有独立进程、Pi session、工作区、事件游标和 registry 记录，同一 AstrBot 会话可同时运行多个 Pi 任务。

Pi 官方 CLI、RPC JSONL 协议和 AstrBot 官方代码均保持原样。本仓库只维护独立适配器和任务桥接层。

## 架构

```text
AstrBot 主模型
    │  pi_agent（短调用，立即返回 task_id）
    ▼
PiTaskService / LLM tool facade
    ▼
TaskScheduler ── PiRpcAdapter ── Pi worker（一个任务一个进程/session）
    │                   │
    └── TaskRegistry ◄──┘  任务元数据、session 路径、生命周期
             │
             └── pi_task_poll/status/session_search（短调用）
```

主要模块：

- `pi_agent_bridge/runtime.py`：WSL/Linux 安装后自动准备内置 runtime。优先解压 `runtime/vendor/pi-runtime-linux-x64.tar.xz`；归档缺失时从 GitHub Release 下载，解压后给 Node 加执行权限并删掉压缩包。仍没有可用包时回退 PATH / nvm。
- `pi_agent_bridge/rpc.py`：公开 Pi RPC 的 JSONL 读写、事件游标、steer、cancel、resume。
- `pi_agent_bridge/registry.py`：SQLite WAL 任务状态、session 路径、进程信息、任务所有者身份、原始回传会话、生命周期和 retention。
- `pi_agent_bridge/scheduler.py`：并发限制、后台观察、worker 生命周期和重启接管。
- `pi_agent_bridge/service.py`：给 AstrBot 工具使用的任务控制、8,000 字符 recent-tail 读取和关键词上下文检索 facade。
- `pi_agent_bridge/context.py`：构造仅包含主模型整理后任务请求的 Pi 初始 prompt，并分别生成稳定的发送者身份和原始会话来源。
- `pi_agent_bridge/provider.py`：读取 AstrBot 选定 Provider 的连接地址、鉴权和模型绑定；将插件显式配置的 PiModelSettings 写入任务专属 Pi `models.json`。
- 当前版本不自动扫描或提炼 workspace 内容；任务文件由 Pi 和用户自行管理。
- `pi_agent_bridge/normal_pipeline.py`：使用 AstrBot 公开 StarTools.create_message/create_event 将 Pi 中间进度和终态通知提交回普通事件 pipeline。插件为自身合成事件注册专用唤醒过滤器，避免群聊前缀被其他唤醒插件当作普通命令拦截；不可用时不直接发送 Pi 内容。
- Pi native session 自动压缩：每个新异步任务都会启用 Pi 官方 `set_auto_compaction`，避免长期上下文无限膨胀。
- Pi 终态唤醒：任务完成、失败、取消或失联时，插件通过 AstrBot 公开事件入口把终态通知重新提交到原会话普通 pipeline；插件不直接发送 Pi 内容。

## 依赖与环境要求

运行时依赖：

- AstrBot `>=4.27.1,<5`，推荐使用 `4.27.1`。插件元数据已声明该范围，不满足时会被 AstrBot 阻止加载。
- 插件内置 linux-x64 Pi CLI `0.84.2` 与 Node.js `22.19.0`；其他平台需在宿主机安装同版本 `pi`。
- 一个可用的 AstrBot OpenAI-compatible Provider/model binding。
- 适配平台当前声明为 `aiocqhttp`（OneBot v11 / QQ）。Linux/WSL x64 是当前主要部署目标；插件也处理 Windows/WSL 路径格式。
- AstrBot 自身的 Python 运行环境和网络访问能力。

插件不修改 Pi 或 AstrBot 官方源码，也不内置或提交 Node/Pi 二进制。Pi CLI 必须由部署环境安装，并确保 AstrBot 服务进程能够执行 `pi --version`。MCP、AstrBot 工具自动继承和非 OpenAI-compatible Provider 不属于当前支持范围。

后台 Pi 使用 `pi_model` 选择的 AstrBot Provider/model 作为模型绑定，但不会自动继承该 Provider 的上下文、推理、输出、模态、采样、成本或兼容字段。Pi 的运行参数全部由以下插件配置项明确控制：`pi_thinking_level`、`pi_context_window`、`pi_max_output_tokens`、`pi_input_modalities`、`pi_temperature`、`pi_top_p`、`pi_top_k`、`pi_min_p` 和 `pi_sampling_params`。填写 0 或留空的数值字段不写入 Pi 配置，由 Pi 使用默认值；Provider 只提供 OpenAI-compatible 连接地址、鉴权和已选模型绑定。

## 配置指南

插件配置由 AstrBot WebUI 根据 `_conf_schema.json` 生成，通常不需要手动编辑 JSON。建议按以下顺序配置：

1. `enable_async_tasks` 保持 `true`，启用独立后台任务桥。
2. `pi_model` 选择 AstrBot 已配置且有余额/权限的 Provider/model。
3. `pi_thinking_level` 默认使用 `max`；用户可以改为 Pi 支持的其他档位。
4. 根据模型能力设置上下文、输出 token、模态和采样参数；填 `0` 的数值字段会被省略。
5. `pi_skill_paths` 和 `pi_extension_paths` 只填写确实存在的绝对路径。
6. `pi_mcp_config_paths` 当前必须保持为空。
7. 修改配置后重启 AstrBot，新的运行参数只影响新创建的 Pi worker。

配置文件为 `_conf_schema.json`，常用项如下：

| 配置项 | 默认值 | 作用 |
| --- | ---: | --- |
| `enable_async_tasks` | `true` | 开启独立的 Pi 后台任务桥。关闭后不创建异步任务 worker。 |
| `pi_model` | `""` | 只选择一个 AstrBot 已配置的 Provider/model 绑定；不再从这里读取或覆盖 Pi 的推理、上下文、输出和采样参数。 |
| `pi_thinking_level` | `max` | Pi 官方 thinking level：`off`、`minimal`、`low`、`medium`、`high`、`xhigh` 或 `max`；实际可用档位由所选模型和 Pi 官方运行时决定。 |
| `pi_context_window` | `0` | Pi 上下文窗口；0 表示不写入，由 Pi 默认值决定。 |
| `pi_max_output_tokens` | `0` | Pi 最大输出 token；0 表示不写入，由 Pi 默认值决定。 |
| `pi_input_modalities` | `["text", "image"]` | Pi 模型输入模态。当前支持 text/image。 |
| `pi_temperature` | `0.5` | Pi temperature。 |
| `pi_top_p` | `1.0` | Pi top-p。 |
| `pi_top_k` | `0` | Pi top-k；0 表示不写入。 |
| `pi_min_p` | `0.0` | Pi min-p；0 表示不写入。 |
| `pi_sampling_params` | `""` | 额外写入 Pi `samplingParams` 的 JSON 参数；留空表示不额外写入。 |
| `state_directory` | `~/.pi/astrbot_plugin_pi_agent` | 桥接状态目录，存放任务数据库、会话、Agent 配置和工作区。 |
| `task_database` | `~/.pi/astrbot_plugin_pi_agent/tasks_v4.db` | 当前版本专用 SQLite 任务注册表路径。数据库必须由当前版本新建，旧版 tasks.db 不兼容。 |
| `workspace_root` | `~/.pi/astrbot_plugin_pi_agent/workspaces` | 任务工作区根目录，每个任务使用独立子目录。 |
| `poll_interval_seconds` | `180` | 后台检活和中间进度更新周期，可配置，不是任务超时。每次最多提供 Pi 原生会话最近 8,000 个字符给主 Agent。 |
| `session_retention_hours` | `24` | 只清理已完成、失败、取消任务的元数据和任务资源。活动/暂停/orphaned 任务不被误删。 |
| `max_concurrent_tasks` | `4` | 同时运行的独立 Pi worker 数量。 |
| `command_timeout_seconds` | `10` | 仅限制 poll/observer 的 `get_state` 和 steer/cancel/resume 等短 RPC 确认；不是任务硬超时或空闲超时。 |
| `pi_skill_paths` | `[]` | 追加的 Pi Skill 目录；每个路径单独一项，填写包含 `SKILL.md` 的目录绝对路径。 |
| `pi_extension_paths` | `[]` | 追加的 Pi 用户扩展文件或目录；每个路径单独一项，按 Pi 官方 `--extension` 参数加载。 |
| `pi_mcp_config_paths` | `[]` | 外部 Pi 扩展或 MCP 配置路径。当前版本不支持加载，必须保持为空。 |

异步任务的 worker 生命周期错误会使任务转为 `failed`。只有 Pi 原生 `agent_end` 才会标为 `completed`；worker 被杀掉或异常退出且没有 `agent_end` 时记为 `orphaned`，下次启动不会自动续跑，需要 `pi_task_resume`。删除操作会取消任务、删除当前 registry 元数据，并清理任务自己的 native session、工作区和 agent 配置目录。当前版本使用全新任务数据库，不读取旧版 registry。

`pi_task_status` 只返回 AstrBot 任务和 native session 元数据，不返回会话内容。`pi_task_poll` 是 AstrBot 主动发起的一次受限 worker 状态检查，并返回最近 8,000 个字符的 native session raw tail；`pi_session_search(session_id, keyword)` 按关键词返回匹配位置上下文，其中 `session_id` 当前传入 `pi_agent` 返回的 `task_id`，总长不超过 8,000 个字符。两者都不做摘要、改写、分类、错误提炼或结果判断。

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
| `pi_session_search(session_id, keyword)` | 按关键词检索对应 Pi 原生 session；`session_id` 当前传入 `pi_agent` 返回的 `task_id`，返回匹配位置上下文，总长度最多 8,000 个字符。 |
| `pi_task_poll(task_id)` | 由 AstrBot 主动请求一次短 Pi 状态检查，并返回最近 8,000 字符的 native session raw tail；不做解释。 |
| `pi_task_follow_up(task_id, message)` | owner 或管理员使用 Pi steer 向活动任务追加要求。 |
| `pi_task_resume(task_id)` | owner 或管理员恢复可恢复任务。 |
| `pi_task_cancel(task_id)` | owner 或管理员取消 worker，但保留任务历史。 |
| `pi_task_delete(task_id)` | owner 或管理员取消并删除任务元数据及任务资源。 |

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

调用 `pi_agent` 后必须立即结束当前 AstrBot tool loop，不要在同一回合调用 `pi_task_poll`、`pi_task_status` 或 `pi_session_search`。后续用户回合需要查询时，最多调用一次 list/status/poll/session_search，工具返回后再次结束当前回合；不要因为状态是 `running` 就在同一回合重复轮询。

## 恢复、保留和删除

只有 Pi 原生会话出现 `agent_end` 才会把任务标为 `completed`。worker 被重启、杀掉或异常退出且没有 `agent_end` 时，任务记为 `orphaned`（中止），不会当成完成，也不会在下次启动时自动续跑；需要用户或主模型显式调用 `pi_task_resume`。AstrBot 或插件重启时，仍在 `running` 且 worker 已退出的任务会从 native session 续跑；worker 仍存活但标准 stdin/stdout RPC 无法安全接管时也会标为 `orphaned`。删除操作会取消任务、删除当前 registry 元数据，并清理任务自己的 native session、工作区和 agent 配置目录。当前版本使用全新任务数据库，不读取旧版 registry。

## 市场分类与兼容声明

- 分类标签：`工具`、`外部集成`。
- 市场分类：`integrations`（外部集成）。
- 适配平台：`aiocqhttp`。
- 支持版本：AstrBot `>=4.27.1,<5`。

## 作者与项目

- 作者：Yezi、Cz
- 项目类型：独立维护的开源 AstrBot 插件
- 许可证：AGPL-3.0
- 个人维护仓库：https://github.com/zhyx111999/astrbot_plugin_pi_agent
- 官方 Pi 项目：https://github.com/earendil-works/pi
- 官方 AstrBot 项目：https://github.com/AstrBotDevs/AstrBot

## 开发与验证

```bash
python -m pytest -q
ruff check main.py pi_agent_bridge tests
python -m compileall -q main.py pi_agent_bridge
git diff --check
```
