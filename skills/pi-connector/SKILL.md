---
name: pi-connector
description: Use Pi as AstrBot's general-purpose Agent executor for code, scripts, research, automation, and long-running multi-step work in an isolated Pi worker while AstrBot remains available for the main conversation.
---

# Pi Agent Bridge

## Core Role

Pi is AstrBot's general-purpose Agent executor and an isolated worker for long-running or multi-step work. It can handle code, scripts, research, automation, file operations, tool-driven workflows, and other engineering or knowledge tasks. Use it for sustained research, coding, testing, multi-agent work, or tasks that should run in the background while AstrBot continues handling other messages.

Calling `pi_agent` returns a `task_id` immediately. AstrBot then uses the task tools to inspect, read, and manage the delegated work. Simple questions and short tool calls belong to AstrBot's own agent.

## Task Creation

Call `pi_agent(prompt, workspace?)` when the requested work is long-running, multi-step, code-oriented, script-oriented, or should continue while AstrBot handles other messages.

The prompt must be a complete, self-contained task instruction. Include the target repository or files, desired behavior, constraints, validation requirements, and expected artifacts when known. The call returns a `task_id` immediately and does not wait for Pi to finish.

At creation time, the new task receives a one-time snapshot of the current AstrBot persona, conversation history, triggering event, user message, available media, and the configured fixed Provider/model. This snapshot belongs to that task's new Pi session. Later inspection, polling, reading, or management by another user never injects the caller's persona, conversation, event, or model into the existing session.

## AstrBot-Controlled Workflow

1. Call `pi_agent` and record the returned `task_id`.
2. Continue the current AstrBot conversation; do not wait for Pi in the same turn.
3. When the user asks about Pi work, call `pi_task_list` to find the relevant task. The directory contains all registered tasks, including tasks owned by other users.
4. Call `pi_task_status` for control metadata without Pi content.
5. Call `pi_task_poll` when AstrBot explicitly needs one short Pi state observation. It returns control metadata only and does not return Pi events.
6. When the user asks to inspect the work, call `pi_task_read(task_id, cursor?, limit?)` to read raw lines from the task's native Pi session JSONL file. The plugin does not parse, summarize, classify, or rewrite these lines; AstrBot reads the complete session itself. Use the returned line cursor to continue.
7. Use `pi_task_follow_up`, `pi_task_resume`, `pi_task_cancel`, or `pi_task_delete` only when the user's request and permissions authorize changing the selected task.
8. AstrBot decides whether to report progress, ask a clarification, continue waiting, provide a result, or take another management action.

Do not repeatedly poll without a reason. Do not infer a failure or a need for user input merely because one observation has no text. The plugin does not create semantic progress summaries or automatically pause a task for lack of meaningful events.

## Async Task Tools

- `pi_agent(prompt: string, workspace?: string)`: Create a new isolated Pi task and return immediately.
- `pi_task_list()`: List all registered async Pi tasks for AstrBot to select.
- `pi_task_status(task_id: string)`: Read AstrBot task control metadata without Pi event content.
- `pi_task_poll(task_id: string)`: Explicitly request one short Pi state observation; return control metadata only.
- `pi_task_read(task_id: string, cursor?: number, limit?: number)`: Read raw native Pi session JSONL lines after a zero-based line cursor. No semantic rewriting is performed.
- `pi_task_result(task_id: string, offset?: number, limit?: number)`: Compatibility alias for `pi_task_read`; `offset` is the native session line cursor.
- `pi_task_follow_up(task_id: string, message: string)`: Send an explicit additional requirement to the existing Pi session.
- `pi_task_resume(task_id: string)`: Resume an existing task/session without rebuilding its original context.
- `pi_task_cancel(task_id: string)`: Cancel a task while retaining its durable history.
- `pi_task_delete(task_id: string)`: Delete a task and its managed resources.
- `pi_session_list()`: List all registered async Pi sessions.
- `pi_session_inspect(session_id: string)`: Read-only inspection of an async task session; legacy session inspection remains administrator-only.
- `pi_session_resume(task_id: string)`: Resume an async task session without rebuilding its context.
- `pi_session_delete(session_id: string)`: Delete an async task session or an administrator-only legacy session.
- `pi_artifact_inspect(task_id: string)`: Read artifact metadata produced by a task.

## Permissions

Read and write permissions are separate:

- Any ordinary user may list, inspect, poll, read, and inspect artifacts for registered tasks, including tasks owned by other users.
- Only the task owner or an AstrBot administrator may send follow-ups, resume, cancel, delete, or otherwise change a task.
- AstrBot administrators may manage every registered task regardless of owner.
- Reading another user's task never changes that task and never injects the reader's context into its Pi session.

## Provider, Context, and Extensions

- All async tasks use the fixed `pi_model` Provider/model selected in the plugin configuration, not the model selected by the current chat.
- Provider fields configured by AstrBot are mapped to the task-local Pi model configuration. Fields that AstrBot does not configure are omitted so Pi uses its own defaults.
- The new task gets the current AstrBot persona, conversation, event, user message, and available media as a creation-time snapshot only.
- `pi_task_read` reads the corresponding native Pi session JSONL directly. The plugin does not build a second content history, summarize errors, classify progress, or extract results from that session.
- Follow-ups add only the explicit message supplied by AstrBot; they do not copy the caller's full AstrBot context.
- AstrBot tools, MCP servers, Skills, and extensions are not inherited automatically. Only paths explicitly configured in `pi_skill_paths` and `pi_extension_paths` are passed to Pi. Pi built-in tools remain enabled.
- Keep `pi_mcp_config_paths` empty because this bridge does not provide a native Pi MCP integration.

## Silent Worker Boundary

Pi's JSONL events are consumed internally only for transport acknowledgements and minimal worker lifecycle transitions. The native Pi session is the sole task-content source exposed to AstrBot. The observer never wakes the main model and never sends completion, failure, progress, or idle notifications. `status` and `poll` expose only task-control metadata; `read` is the explicit native-session channel.

## Legacy Synchronous Route

The compatibility tools remain separate from the silent async worker route: `pi_open_session`, `pi_list_sessions`, `pi_resume_session`, `pi_send_message`, `pi_get_session_info`, `pi_run_command`, `pi_abort`, and `pi_reply_ui`. They provide direct administrator-only interactive Pi sessions and should not be used for normal background task delegation.
