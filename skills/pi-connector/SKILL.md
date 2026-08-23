---
name: pi-connector
description: Delegate long-running research, coding, and multi-step work to an isolated Pi worker from AstrBot, then observe it with short polling calls while the main conversation remains available.
---

# Pi Agent Bridge

## Use Pi when

Call `pi_agent` for work that is long-running, multi-step, or naturally isolated:

- repository research, coding, tests, and file changes;
- several dependent tool calls or a long-running command;
- independent parallel subtasks;
- a user explicitly asks for background or continuous execution.

Keep ordinary questions, short rewrites, and one quick tool call in AstrBot's own agent. Pi is a worker, not a replacement for the main conversational model.

## Non-blocking workflow

1. Call `pi_agent(prompt, workspace?)` with a complete, self-contained instruction. The call returns a `task_id` immediately.
2. Continue the current conversation normally. Do not wait for Pi to finish inside the same turn and do not repeatedly call a long-running operation.
3. In a later model turn, call `pi_task_poll(task_id)` to drain local buffered events and read the latest local observation. The tool does not wait for Pi or request remote state; the background observer performs the configured remote state request. Use `pi_task_status` for durable state, `pi_task_result` for the latest snapshot content plus persisted artifacts, or `pi_artifact_inspect` for produced files/media.
4. If `has_new_meaningful_event` is true, summarize the new progress. Protocol heartbeats, empty JSONL records, and duplicate snapshots are not meaningful events.
5. If status is `needs_user_decision`, tell the user that there has been no meaningful progress for the configured number of observation cycles. Ask whether to continue, add a requirement, inspect, cancel, or delete; do not kill the task automatically.
6. Use `pi_task_follow_up(task_id, message)` for an additional user requirement. Use `pi_task_resume(task_id)` to resume a logically paused/orphaned task, and use cancel/delete only when requested or clearly required.

There is no hard task timeout and no idle timeout. `command_timeout_seconds` bounds only the background observer's short `get_state` request and steer/cancel/resume acknowledgements; it never terminates a Pi worker. `pi_task_poll` reads local buffered events and the latest durable snapshot without waiting for Pi. Each task owns a separate Pi process, session, workspace, event cursor, and latest snapshot, so multiple tasks can run for one AstrBot conversation without a conversation lock.

## Async task tools

- `pi_agent(prompt: string, workspace?: string)` — create an isolated task and return immediately.
- `pi_task_status(task_id: string)` — read status, owner-scoped metadata, and latest snapshot.
- `pi_task_list()` — list tasks visible to the current user; only an administrator with global management enabled sees all tasks.
- `pi_task_result(task_id: string)` — read the latest snapshot content and persisted artifacts.
- `pi_task_poll(task_id: string)` — drain local buffered events and return the latest observation without a remote Pi request.
- `pi_task_follow_up(task_id: string, message: string)` — steer an active worker with an added requirement.
- `pi_task_resume(task_id: string)` — resume a paused or recoverable task.
- `pi_task_cancel(task_id: string)` — cancel the worker while retaining history.
- `pi_task_delete(task_id: string)` — cancel and remove task-owned resources.
- `pi_session_list()` — list sessions represented by visible tasks.
- `pi_session_inspect(task_id: string)` — inspect a task's Pi session.
- `pi_session_resume(task_id: string)` — resume that session through the task bridge.
- `pi_session_delete(task_id: string)` — remove that session and its task resources.
- `pi_artifact_inspect(task_id: string)` — inspect text, JSON, Markdown, files, and media artifacts.

- All async task tools are owner-scoped by default. Every user can inspect and manage their own tasks, but cannot inspect, steer, resume, cancel, delete, or inspect artifacts belonging to another user. When `task_require_admin` is enabled, AstrBot administrators may manage any user's tasks; ordinary users remain owner-scoped.

## Context, provider, Skill, and MCP

- At creation time Pi receives the configured AstrBot persona (when enabled) plus a one-time snapshot of the current event's public fields and source message. Later messages are not synchronized automatically; use `pi_task_follow_up`.
- All background Pi tasks use the single fixed `pi_model` selection, which points to an already-configured AstrBot Provider/model instance. The worker never follows the provider or model selected for the current chat. The adapter accepts OpenAI-compatible providers only. API keys are kept in the worker environment and must not be copied into prompts, task metadata, snapshots, or replies.
- Pi official code and RPC remain unchanged. Each configured Skill directory is passed to the worker through a repeated public CLI argument (`--skill <path>`). This proves only that the path was supplied, not that Pi loaded the Skill; rely on the task snapshot/result for runtime evidence.
- Pi `0.84.2` RPC has no native MCP bridge. AstrBot MCP servers are never inherited automatically. At present, any non-empty `pi_mcp_config_paths` setting makes `pi_agent` return a structured unsupported-capability envelope and the paths are not passed to Pi. Do not expose a raw exception or claim that MCP is available.

## Safety and user control

Confirm the target workspace before delegating. Pi inherits the AstrBot process permissions and can execute commands or modify files. Do not approve destructive commands, credential access, or broad filesystem changes without explicit user intent. When Pi requests extension UI input, preserve the request for the user instead of fabricating confirmation.

## Legacy synchronous route

For an existing interactive session, the compatibility tools remain available: `pi_open_session`, `pi_list_sessions`, `pi_resume_session`, `pi_send_message`, `pi_get_session_info`, `pi_run_command`, `pi_get_available_commands`, `pi_abort`, and `pi_reply_ui`. These are separate from the non-blocking `pi_agent` task workflow.
