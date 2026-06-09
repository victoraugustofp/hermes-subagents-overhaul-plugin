# Research: ACP tool→event bridge in `hermes-acp-plugin` (for WS2)

Validated against Hermes 0.16.0 `acp_adapter/{server,events,session}.py`,
`agent/async_utils.py`, `hermes_cli/plugins.py`, `gateway/session_context.py`, and the
existing `hermes_acp_plugin/patch/extended_context.py`. ACP SDK 0.9.0.
Official: https://agentclientprotocol.com/protocol/v1/{overview,prompt-turn,content,extensibility}
and Hermes ACP internals: https://hermes-agent.nousresearch.com/docs/developer-guide/acp-internals

## What WS2 adds (in the sibling repo, NOT this one)
- `hermes_acp_plugin/runtime.py` — a process-global, **session-lifetime** registry
  `session_id -> (conn, loop, request_permission)`; `register_session`,
  `unregister_session`, `current_acp_session() -> AcpSessionHandle | None`.
- `hermes_acp_plugin/patch/tool_session_access.py` — `apply(ctx)` `patch.wrap`s
  `HermesACPAgent.prompt` to `register_session(...)` at entry.

## Key facts
- `HermesACPAgent.prompt(self, prompt, session_id, **kwargs)` (`server.py:1290`).
  `self._conn` is the ACP connection (set in `on_connect`, `:526`); `loop =
  asyncio.get_running_loop()` (`:1384`). The sync agent loop runs in
  `loop.run_in_executor(_executor, ctx.run, _run_agent)` (`:1553`) — so tool code runs
  on a worker thread while `loop` stays free to service emissions.
- Emissions cross thread->loop via
  `agent.async_utils.safe_schedule_threadsafe(conn.session_update(session_id, update),
  loop)` (see `events.py::_send_update`). `conn.session_update(...)` and
  `conn.request_permission(...)` are **async** (await on the loop).
- **V-7**: `safe_schedule_threadsafe(coro, loop)` is safe from a long-lived background
  thread for the whole session (single server loop). It returns `None` if the loop is
  gone (closes the coro) — callers degrade gracefully. Capture `loop` at registration.
- **V-4 (teardown)**: there is NO clean "session closed" hook. `cancel()`
  (`server.py:1209`) only interrupts; `SessionManager.remove_session` (`session.py:244`)
  and `cleanup` (`:368`) exist. Strategy: register at prompt entry (idempotent, refresh
  `(conn,loop)`) and **do NOT unregister in `prompt`'s `finally`** (background subagents
  outlive the turn). Unregister via: (a) wrapping `remove_session`/`cleanup` if reachable,
  (b) session-not-found fallback when `session_update` errors, (c) a TTL. Also optionally
  wrap `cancel`/teardown to call `SubagentManager.cancel_all(session_id)` (soft-import).
- **V-3**: `PluginContext.inject_message` returns `False` under ACP (no CLI ref) — it
  cannot self-initiate a server turn. So the reliable wake-up is the `pre_llm_call` hook
  (already implemented in this repo's `contrib/hooks.py`). `inject_message` is best-effort
  CLI/TUI only. **`on_session_end` plugin hook is NOT currently fired** — ACP cleanup must
  go through the teardown wrap, not the hook.
- `patch.wrap(cls, name, factory)` chains and is idempotent per wrapper identity;
  `extended_context.py` already wraps `prompt` (ORDER ~50) -> use ORDER 60. ALWAYS call
  `inner(*a, **kw)` and return its result.
- `session_id` for tools is read via
  `gateway.session_context.get_session_env("HERMES_SESSION_ID")` (+ `os.environ`).

## ACP SDK emit helpers (used by `events.py`)
`acp.start_tool_call(tool_call_id, title, kind=, status=, content=, locations=,
raw_input=)`, `acp.update_tool_call(tool_call_id, title=, kind=, status=, content=,
result=, error=)`, `acp.tool_content(block)`, `acp.tool_diff_content(path, new_text,
old_text=)`, `acp.text_block(text)`, `acp.update_agent_message_text/agent_thought_text`.

## `AcpSessionHandle` (what `current_acp_session()` returns)
Thread-safe, fire-and-forget (via `safe_schedule_threadsafe`):
- `start_tool_call(tool_call_id, title, kind="other", status="in_progress", **fields)`
- `update_tool_call(tool_call_id, status=None, content=None, raw_output=None, **fields)`
- `request_permission(tool_call, options) -> outcome` (blocking with timeout; for
  foreground relay — schedule `conn.request_permission` and wait on the returned future)
- `plan(entries)`

This repo's `sinks/acp_sink.py` soft-imports `current_acp_session()` and maps
`SubagentEvent`s onto these per PLAN.md §7. No hard dependency either direction.
