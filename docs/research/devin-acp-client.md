# Research: Devin `devin acp` as an ACP client (for the Devin backend / WS3)

Validated against Hermes 0.16.0 `agent/copilot_acp_client.py`, the installed `acp`
SDK 0.9.0, and `devin` 2026.5.26-7. Official:
- ACP overview: https://agentclientprotocol.com/protocol/v1/overview
- ACP prompt turn: https://agentclientprotocol.com/protocol/v1/prompt-turn
- ACP content: https://agentclientprotocol.com/protocol/v1/content
- ACP Python SDK: https://agentclientprotocol.github.io/python-sdk/
- Devin Desktop custom ACP agent: https://docs.devin.ai/desktop/acp-custom

## Spawning `devin acp`
`devin acp` is a native ACP server over stdio. Its only subcommand flag is
`--agent-type`. **Model/sandbox/permission are top-level options** and may either
precede the subcommand or be set via env (both confirmed against the binary):
- `devin --model <m> [--sandbox] [--permission-mode auto|dangerous] acp`
- env: `DEVIN_MODEL`, `DEVIN_SANDBOX`, `DEVIN_PERMISSION_MODE` (prefer env for a
  spawned child — robust regardless of arg-order).
Config file: `~/.config/devin/config.json` has `agent.model`, `permissions.allow`
(scopes like `Write(path)`, `Read(path)`, `Fetch(domain:...)`), `acp:true`.

### V-2 read-only posture
`--permission-mode auto` auto-approves only read-only tools (writes need approval);
`--sandbox` enforces OS-level Read/Write scopes. For `read_only` profiles use
`DEVIN_PERMISSION_MODE=auto` (and grant no `Write(...)` scopes); optionally `--sandbox`.

## Existing client: `CopilotACPClient` (`copilot_acp_client.py:334`)
Standalone (no AIAgent coupling). `__init__(*, acp_command=None, acp_args=None,
acp_cwd=None, ...)`. `_run_prompt()` (`:438`) does the sync stdio JSON-RPC loop:
`initialize` (`:550`) -> `session/new {cwd, mcpServers}` (`:567`) ->
`session/prompt {sessionId, prompt:[{type:"text",text}]}` (`:580`), reading
`session/update` notifications inline (`:611`).

**Limitations for us:** it only collects `agent_message_chunk` / `agent_thought_chunk`
text and **auto-denies** inbound `session/request_permission` (`_permission_denied`,
`:123`/`:631`). It does NOT surface `tool_call`/`tool_call_update`/`plan`/diffs.

## Recommendation: vendor a fuller sync ACP client
Reuse the stdio/JSON-RPC framing pattern but surface ALL updates + a permission
callback. Either:
- (A) adapt the `_run_prompt` loop: keep a `next_id`, write requests, read stdout lines,
  and on `session/update` translate the `update.sessionUpdate` kind into a
  `SubagentEvent` pushed to a `queue.Queue`; on inbound `session/request_permission`
  invoke a resolver and reply `{outcome:{outcome:"selected", optionId}}` /
  `{outcome:{outcome:"cancelled"}}`; OR
- (B) drive the async `acp` SDK client: `acp.stdio` connection + `ClientSideConnection`
  (`initialize`, `new_session`, `prompt`); run it on a private event loop in a worker
  thread and bridge to a sync `queue.Queue` for `events()`.
Prefer (A) for a synchronous `events()` with the manager. Keep the child process,
session_id, and a cancel (`session/cancel` or terminate) on the handle.

## `session/update` payload kinds (ACP SDK `acp.schema`)
`sessionUpdate` ∈ {`agent_message_chunk`, `agent_thought_chunk`, `tool_call`,
`tool_call_update`, `plan`, `available_commands_update`, ...}.
- `tool_call`: `toolCallId, title, kind, status:"pending", rawInput, locations`.
- `tool_call_update`: `toolCallId, status:in_progress|completed|failed, content, rawOutput`.
- content blocks: `{type:"text",text}`, diff via `FileEditToolCallContent`.
Map these to `SubagentEvent` (`tool_call`/`tool_update`/`diff`/`message`/`thought`/`plan`).

## Prompt stop reasons
`PromptResponse.stopReason ∈ {end_turn, max_tokens, max_turn_requests, refusal,
cancelled}` -> `SubagentResult.exit_reason`.

## Backend mapping to our contract
`start(task, profile, cwd, resume_handle)`:
- spawn `devin acp` with model/sandbox/permission via env (+ flags),
- ACP handshake; `session/prompt(task)`; translate updates -> queue of `SubagentEvent`s,
- inbound `session/request_permission` -> yield `kind="permission"` event with a minted
  `request_id` + the offered `options`; block the reader until `answer_permission`,
- `backend_session_id` = child `sessionId` (resume via `session/load` or `devin -r`),
- `result()` -> `SubagentResult(summary=joined agent_message text, exit_reason=stopReason)`.
