---
name: pi-agent
description: Use Pi as AstrBot's general-purpose Agent executor for code, scripts, research, automation, and long-running multi-step work in an isolated Pi worker while AstrBot remains available for the main conversation.
---

# Pi Agent

## Core Role

This plugin requires AstrBot's built-in Agent capability. Keep AI chat enabled, set the Agent runner to the local/built-in AstrBot Agent, and leave function/tool calling on. Third-party runners such as Dify, Coze, DashScope, or DeerFlow cannot dispatch `pi_agent`.

Pi is AstrBot's general-purpose Agent executor and the default executor for tasks that are not extremely simple and purely conversational. It can handle code, scripts, research, automation, file operations, media/file handling, testing, tool-driven workflows, and other engineering or knowledge tasks.

Use `pi_agent` by default whenever the task involves tools, files, external information, execution, validation, or non-trivial reasoning, even if the task may be completed quickly. Only very simple one-turn conversation, such as a basic explanation, translation, short rewrite, or casual reply with no tool/file work, should stay in AstrBot's own Agent.

Calling `pi_agent` returns a `task_id` immediately. When the task reaches completed, failed, cancelled, or orphaned, the plugin submits an AstrBot wake event into the owning conversation's normal event pipeline. The main model receives a bounded native-session tail and decides whether to reply, send a file, or manage the task further. The plugin never sends the Pi result directly.

## Task Creation

Call `pi_agent(prompt, workspace?)` when the requested work is long-running, multi-step, code-oriented, script-oriented, or should continue while AstrBot handles other messages.

The prompt must be a complete, self-contained task instruction. Include the target repository or files, desired behavior, constraints, validation requirements, and expected artifacts when known. The call returns a `task_id` immediately and does not wait for Pi to finish.

At creation time, the new Pi session receives only the main model's complete, already-refined task request plus the selected model binding and explicit Pi runtime settings. AstrBot persona, full system prompts, conversation history, raw events, media context, and viewer context are not injected into the Pi session. Later inspection or management never injects any caller context.

Terminal wakeups are submitted through AstrBot's public event factory into the original session's normal event pipeline. After receiving an intermediate or terminal Pi session tail, the main Agent must never forward raw Pi session text, JSONL, tool logs, command output, internal status, or stack traces. If a user-visible reply is needed, produce one concise, natural, interpreted reply through the normal response pipeline; send files through the normal agent tools with a short explanation. If there is no meaningful user-visible result, do not send a message.

## AstrBot-Controlled Workflow

1. Call `pi_agent` and record the returned `task_id`.
2. Continue the current AstrBot conversation; after `pi_agent` returns, end the current tool loop immediately. Do not poll or search the task in the same turn.
3. When a later user turn requires Pi information, call `pi_task_list` or the known task tool. After each list/status/poll/search call, end the current tool loop; never chain polling calls in one turn.
4. Call `pi_task_status` for control metadata without Pi content.
5. Call `pi_task_poll` when AstrBot explicitly needs one short Pi state observation. It returns control metadata plus an unchanged native-session tail capped at 8,000 characters; it does not summarize or interpret Pi events.
6. For keyword inspection, call `pi_session_search(session_id, keyword)` to receive matching native-session context capped at 8,000 characters. The current `session_id` argument is the `task_id` returned by `pi_agent`; the plugin does not parse, summarize, classify, or rewrite it.
7. Use `pi_task_follow_up`, `pi_task_resume`, `pi_task_cancel`, or `pi_task_delete` only when the user's request and permissions authorize changing the selected task.
8. AstrBot decides whether to report progress, ask a clarification, continue waiting, provide a result, or take another management action.

The plugin does not create semantic progress summaries or automatically pause a task for lack of meaningful events. A `running` result is not an instruction to poll again in the same turn; end the turn and let the next user/model turn decide when to inspect again. If AstrBot or the plugin reloads, a still-`running` task may be restarted from its native session. A worker that exited without `agent_end` is marked `orphaned` and is not auto-resumed; use `pi_task_resume` only when the user asks to continue. Terminal wakeups are only for completed, failed, cancelled, and orphaned states.

## Async Task Tools

- `pi_agent(prompt: string, workspace?: string)`: Create a new isolated Pi task and return immediately.
- `pi_task_list()`: List all registered async Pi tasks for AstrBot to select.
- `pi_task_status(task_id: string)`: Read AstrBot task control metadata without Pi event content.
- `pi_task_poll(task_id: string)`: Explicitly request one short Pi state observation and receive an unchanged native-session tail capped at 8,000 characters.
- `pi_session_search(session_id: string, keyword: string)`: Search a native Pi session using the `task_id` returned by `pi_agent` as `session_id`, and return matching context capped at 8,000 characters.
- `pi_task_follow_up(task_id: string, message: string)`: Send an explicit additional requirement to the existing Pi session.
- `pi_task_resume(task_id: string)`: Resume an existing task/session without rebuilding its original context.
- `pi_task_cancel(task_id: string)`: Cancel a task while retaining its durable history.
- `pi_task_delete(task_id: string)`: Delete a task and its managed resources.

## Permissions

Read and write permissions are separate:

- Any ordinary user may list, inspect, poll, and search registered tasks, including tasks owned by other users.
- Only the task owner or an AstrBot administrator may send follow-ups, resume, cancel, delete, or otherwise change a task.
- AstrBot administrators may manage every registered task regardless of owner.
- Reading another user's task never changes that task and never injects the reader's context into its Pi session.

## Provider, Context, and Extensions

- All async tasks use the Provider/model binding selected by `pi_model`, not the model selected by the current chat.
- Pi runtime behavior is controlled by plugin settings: `pi_thinking_level`, `pi_context_window`, `pi_max_output_tokens`, `pi_input_modalities`, `pi_temperature`, `pi_top_p`, `pi_top_k`, `pi_min_p`, and `pi_sampling_params`.
- The selected AstrBot Provider supplies only the OpenAI-compatible connection, credentials, Provider binding, and model identity. Its reasoning, context, output, modality, sampling, cost, and compatibility fields are not copied automatically.
- Empty or zero numeric plugin settings are omitted so Pi uses its own defaults.
- Every new Pi task enables Pi's native automatic context compaction; this is a Pi runtime setting and does not cause AstrBot to summarize or rewrite the session.
- The new task receives only the main model's refined request. AstrBot persona, system prompt, conversation history, raw event, and media context are not copied into Pi.
- `pi_session_search` returns only bounded context around a literal keyword, capped at 8,000 characters. The plugin does not build a second content history, summarize errors, classify progress, or extract results.
- Follow-ups add only the explicit message supplied by AstrBot; they do not copy the caller's full AstrBot context.
- AstrBot tools, MCP servers, Skills, and extensions are not inherited automatically. Only paths explicitly configured in `pi_skill_paths` and `pi_extension_paths` are passed to Pi. Pi built-in tools remain enabled.
- Keep `pi_mcp_config_paths` empty because this bridge does not provide a native Pi MCP integration.

## Silent Worker Boundary

Pi's JSONL events are consumed internally only for transport acknowledgements and minimal worker lifecycle transitions. The native Pi session is the sole task-content source exposed to AstrBot. The observer wakes the main model only when a changed native-session tail is available, at the configurable interval. `status` exposes task-control metadata; `poll` and `pi_session_search` expose bounded raw session context capped at 8,000 characters. A task becomes `completed` only after Pi emits `agent_end`. If the worker exits or is killed without that event, the task is marked `orphaned` and waits for an explicit `pi_task_resume`.
