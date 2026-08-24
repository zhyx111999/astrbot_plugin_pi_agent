---
name: pi-agent
description: Use Pi as AstrBot's general-purpose Agent executor for code, scripts, research, automation, and long-running multi-step work in an isolated Pi worker while AstrBot remains available for the main conversation.
---

# Pi Agent

## Core Role

Pi is AstrBot's general-purpose Agent executor and the default executor for tasks that are not extremely simple and purely conversational. It can handle code, scripts, research, automation, file operations, media/file handling, testing, tool-driven workflows, and other engineering or knowledge tasks.

Use `pi_agent` by default whenever the task involves tools, files, external information, execution, validation, or non-trivial reasoning, even if the task may be completed quickly. Only very simple one-turn conversation, such as a basic explanation, translation, short rewrite, or casual reply with no tool/file work, should stay in AstrBot's own Agent.

Calling `pi_agent` returns a `task_id` immediately. When the task reaches completed, failed, cancelled, or orphaned, the plugin schedules an AstrBot native active-agent wakeup for the owning conversation. The awakened main model then reads the Pi session itself and decides whether to reply, send a file, or manage the task further. The plugin never sends the Pi result directly.

## Task Creation

Call `pi_agent(prompt, workspace?)` when the requested work is long-running, multi-step, code-oriented, script-oriented, or should continue while AstrBot handles other messages.

The prompt must be a complete, self-contained task instruction. Include the target repository or files, desired behavior, constraints, validation requirements, and expected artifacts when known. The call returns a `task_id` immediately and does not wait for Pi to finish.

At creation time, the new Pi session receives only the main model's complete, already-refined task request plus the selected model binding and explicit Pi runtime settings. AstrBot persona, full system prompts, conversation history, raw events, media context, and viewer context are not injected into the Pi session. Later inspection or management never injects any caller context.

## AstrBot-Controlled Workflow

1. Call `pi_agent` and record the returned `task_id`.
2. Continue the current AstrBot conversation; after `pi_agent` returns, end the current tool loop immediately. Do not poll or read the task in the same turn.
3. When a later user turn requires Pi information, call `pi_task_list` or the known task tool. After each list/status/poll/read call, end the current tool loop; never chain polling calls in one turn.
4. Call `pi_task_status` for control metadata without Pi content.
5. Call `pi_task_poll` when AstrBot explicitly needs one short Pi state observation. It returns control metadata only and does not return Pi events.
6. For normal inspection, call `pi_task_read(task_id)` to receive only the recent 50,000-character tail of the native Pi session. The plugin does not parse, summarize, classify, or rewrite it.
7. Only when the user explicitly asks for the complete session, call `pi_task_read_full(task_id, cursor?, limit?)` for line-based full-session pages. After either read tool returns, end the current tool loop; do not immediately request another page unless the user explicitly needs it.
8. Use `pi_task_follow_up`, `pi_task_resume`, `pi_task_cancel`, or `pi_task_delete` only when the user's request and permissions authorize changing the selected task.
9. AstrBot decides whether to report progress, ask a clarification, continue waiting, provide a result, or take another management action.

The plugin does not create semantic progress summaries or automatically pause a task for lack of meaningful events. A `running` result is not an instruction to poll again in the same turn; end the turn and let the next user/model turn decide when to inspect again. Terminal wakeups are only for completed, failed, cancelled, and orphaned states.

## Async Task Tools

- `pi_agent(prompt: string, workspace?: string)`: Create a new isolated Pi task and return immediately.
- `pi_task_list()`: List all registered async Pi tasks for AstrBot to select.
- `pi_task_status(task_id: string)`: Read AstrBot task control metadata without Pi event content.
- `pi_task_poll(task_id: string)`: Explicitly request one short Pi state observation; return control metadata only.
- `pi_task_read(task_id: string)`: Read only the recent 50,000-character tail of the native Pi session. No semantic rewriting is performed.
- `pi_task_read_full(task_id: string, cursor?: number, limit?: number)`: Explicitly read complete native Pi session lines by cursor when the user asks for full history.
- `pi_task_result(task_id: string)`: Compatibility alias for the bounded recent-session reader.
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

- All async tasks use the Provider/model binding selected by `pi_model`, not the model selected by the current chat.
- Pi runtime behavior is controlled by plugin settings: `pi_thinking_level`, `pi_context_window`, `pi_max_output_tokens`, `pi_input_modalities`, `pi_temperature`, `pi_top_p`, `pi_top_k`, `pi_min_p`, and `pi_sampling_params`.
- The selected AstrBot Provider supplies only the OpenAI-compatible connection, credentials, Provider binding, and model identity. Its reasoning, context, output, modality, sampling, cost, and compatibility fields are not copied automatically.
- Empty or zero numeric plugin settings are omitted so Pi uses its own defaults.
- Every new async and legacy Pi session enables Pi's native automatic context compaction; this is a Pi runtime setting and does not cause AstrBot to summarize or rewrite the session.
- The new task receives only the main model's refined request. AstrBot persona, system prompt, conversation history, raw event, and media context are not copied into Pi.
- `pi_task_read` reads only the recent native session tail by default. `pi_task_read_full` is the explicit complete-session path. The plugin does not build a second content history, summarize errors, classify progress, or extract results from either path.
- Follow-ups add only the explicit message supplied by AstrBot; they do not copy the caller's full AstrBot context.
- AstrBot tools, MCP servers, Skills, and extensions are not inherited automatically. Only paths explicitly configured in `pi_skill_paths` and `pi_extension_paths` are passed to Pi. Pi built-in tools remain enabled.
- Keep `pi_mcp_config_paths` empty because this bridge does not provide a native Pi MCP integration.

## Silent Worker Boundary

Pi's JSONL events are consumed internally only for transport acknowledgements and minimal worker lifecycle transitions. The native Pi session is the sole task-content source exposed to AstrBot. The observer never wakes the main model and never sends completion, failure, progress, or idle notifications. `status` and `poll` expose only task-control metadata; `read` is the explicit native-session channel.

## Legacy Synchronous Route

The legacy tools are synchronous and may wait for a Pi response. Use them only when the user explicitly asks for `/pi` or an existing interactive legacy session. Never use `pi_open_session` or `pi_send_message` for normal background delegation.
