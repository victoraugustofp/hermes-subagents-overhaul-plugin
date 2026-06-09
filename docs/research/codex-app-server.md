# Research: Codex `app-server` transport (for the Codex backend / WS1)

Validated against Hermes 0.16.0 reference at
`../hermes-acp-plugin/.reference/hermes-agent/` and the installed `codex` 0.137.0.
Re-validate per release. Official: https://github.com/openai/codex (app-server is
experimental; protocol moves fast — pin a tested codex version).

## Reuse, don't rewrite
Drive Hermes' existing standalone client (zero AIAgent coupling):

```python
from agent.transports.codex_app_server_session import CodexAppServerSession
from agent.transports.codex_app_server import CodexAppServerClient        # lower level
from agent.transports.codex_event_projector import CodexEventProjector    # optional
```

### `CodexAppServerClient.__init__` (`codex_app_server.py:69`)
`(codex_bin="codex", codex_home=None, extra_args=None, env=None)`.
Spawns `codex app-server` (+ `extra_args`), pipes stdio, starts daemon reader threads.
`env` is merged over `os.environ`; `codex_home` sets `CODEX_HOME`.

### `CodexAppServerSession.__init__` (`codex_app_server_session.py:197`, keyword-only)
`(cwd=None, codex_bin="codex", codex_home=None, permission_profile=None,
approval_callback=None, on_event=None, request_routing=None, client_factory=None)`
- `approval_callback(command_label, description, allow_permanent) -> "once"|"session"|"always"|"deny"`.
  `None` => fail-closed (auto-decline).
- `on_event(notification_dict)` fires for EVERY notification (this is our event stream).
- `request_routing`: auto-approve policy for non-interactive contexts.

## Lifecycle
1. `thread_id = session.ensure_started()` -> JSON-RPC `thread/start {cwd}`; returns
   `result.thread.id | thread.sessionId | sessionId | threadId`. **This is the
   resumable id (= our `backend_session_id`).**
2. `result = session.run_turn(user_input, turn_timeout=600.0,
   notification_poll_timeout=0.25, post_tool_quiet_timeout=90.0)` — **blocks**, drains
   the stream, calls `on_event` per notification, bridges approvals via
   `approval_callback`, returns a `TurnResult`.

### `TurnResult` (`codex_app_server_session.py:64`)
`final_text, projected_messages, tool_iterations, interrupted, error, turn_id,
thread_id, should_retire`.

## Event stream (notifications -> `on_event`)
- `turn/started` `{threadId, turn:{id,...}}`
- `item/started` `{threadId, turnId, item:{type,id,...}}`
- `item/<type>/delta` streaming text (`item.delta`)
- `item/completed` `{item:{type,id,...}}` — the materialized ones:
  - `agentMessage` -> `item.text` (assistant message; sets `final_text`)
  - `reasoning` -> `item.summary[]`, `item.content[]` (thought)
  - `commandExecution` -> `item.command,cwd,status,aggregatedOutput,exitCode` (tool/execute)
  - `fileChange` -> `item.changes[] = {kind:{type:add|update|delete}, path, before?, after?}` (diff/edit)
  - `mcpToolCall`, `dynamicToolCall` -> tool calls with `arguments`, `result`/`error`
- `turn/completed` `{turn:{id,status:"completed"|"interrupted"|"failed", error?}}` — terminal
- `thread/tokenUsage/updated` `{usage:{inputTokens,outputTokens,totalTokens,...}}`

## Approvals (server-initiated requests; bridged by `approval_callback`)
`item/commandExecution/requestApproval {command,cwd,reason}`,
`item/fileChange/requestApproval {itemId,reason,grantRoot}`,
`item/permissions/requestApproval`, `mcpServer/elicitation/request`.
Decision wire values: `accept | acceptForSession | decline`
(`once->accept`, `session/always->acceptForSession`, else `decline`).

## Model / sandbox mapping (`ResolvedProfile`)
Pass via `extra_args`: `["-c", f'model="{model}"', "-c", f'sandbox_mode="{sandbox}"']`
where `sandbox in {read-only, workspace-write, danger-full-access}` (confirmed by
`codex exec --help`: `-s/--sandbox`, `-m/--model`, `-C/--cd`, `--json`). `read_only`
profiles => `sandbox_mode="read-only"`. (Or set `permission_profile`.)

## Resume
Store `thread_id`. App-server thread-resume isn't wired in Hermes yet; fallback is the
CLI: `codex exec resume <thread-id>` / `--last`. For WS1, resume MAY use a fresh
`thread/start` + replayed context if app-server resume is unavailable on the pinned
codex — document the choice.

## Backend mapping to our contract
`start(task, profile, cwd, resume_handle)`:
- build `CodexAppServerSession(cwd=cwd, extra_args=[...model/sandbox...],
  approval_callback=<rendezvous>, on_event=<translate->queue>)`,
- spawn a worker thread running `ensure_started()` + `run_turn(task)`,
- `on_event` translates each notification into a `SubagentEvent` (`message`/`thought`/
  `tool_call`/`tool_update`/`diff`/`usage`) pushed on a `queue.Queue`; `events()` yields
  until a sentinel; `result()` builds `SubagentResult` from `TurnResult`
  (`backend_session_id=thread_id`, `exit_reason` from turn status, `files_changed` from
  fileChange items).
- `approval_callback` blocks the run_turn thread; the manager calls
  `answer_permission(request_id, outcome)` to release it (yield a `kind="permission"`
  event carrying a `request_id` you mint per request).
