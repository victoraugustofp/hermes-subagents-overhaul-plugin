# hermes-subagents-overhaul-plugin — End-to-End Implementation Plan

Goal: give Hermes a **first-class subagent system modeled exactly on the Devin CLI**
(`run_subagent` / `read_subagent`, foreground **and** background), where the subagents
are **real external AI-agent processes** — **Devin CLI** and **OpenAI Codex** instances —
**not** in-process Hermes `AIAgent` children (which is what the built-in `delegate_task`
spawns today). Subagents are spawned **via tools** (never by the model shelling out to a
terminal), so their lifecycle and activity can be surfaced as **proper ACP events** for
both foreground and background subagents, through the companion
`hermes-acp-plugin`.

This document is the build spec for **two coordinated repos**:

| | Repo | Role |
|---|---|---|
| **Project A** | `hermes-subagents-overhaul-plugin` (this repo) | A Hermes plugin that registers `run_subagent` / `read_subagent`, manages external Devin/Codex subagent processes, and emits subagent activity to a pluggable **event sink** (CLI/TUI progress callback **or** ACP). Platform-agnostic. |
| **Project B** | `hermes-acp-plugin` (sibling repo) | Adds the missing ACP plumbing: a generic, session-scoped **tool→ACP event emitter** so any tool (here: the subagent tools) can stream `session/update` notifications (`tool_call` / `tool_call_update` / permission forwarding) for the whole session lifetime — including from background threads that outlive a turn. |

> Analysis basis (re-verify per release): `NousResearch/hermes-agent` @ `0.16.0`,
> `agent-client-protocol==0.9.0`, ACP run via `acp.run_agent(agent, use_unstable_protocol=True)`.
> A reference clone is checked out at `../hermes-acp-plugin/.reference/hermes-agent/`
> (the exact code installed in that repo's dev venv). All `file:line` references below
> point there and **must be re-validated per Hermes release**.

---

## 0. Decisions (locked — do not relitigate)

These were confirmed with the project owner before this plan was written:

1. **Coexist with `delegate_task`.** Register **new** `run_subagent` / `read_subagent`
   tools. Leave the built-in `delegate_task` (in-process Hermes children) untouched. The
   model picks the right tool per task.
2. **Profiles map to backend + model + settings.** The Devin-style `profile` attribute
   selects the **backend** (`devin` | `codex`), the **model**, the **sandbox / permission
   posture**, and any extra launch flags. There is **no** separate `backend` argument on
   the tool — the schema stays identical to Devin's published spec.
3. **Rich native transport.** Spawn `devin acp` (native ACP server) and `codex app-server`
   (native JSON-RPC streaming). **Foreground** subagents forward their granular tool-calls,
   diffs, and permission prompts into the parent ACP session; **background** subagents
   collapse to a single streaming `tool_call` with periodic `tool_call_update`s + a final
   result (matching Devin's "you don't see the raw output; the parent summarizes").
4. **True cross-turn background.** Background subagents outlive the spawning turn; on
   completion they (a) finalize their ACP `tool_call`, and (b) deliver a
   `<subagent_completion_notification>`-style wake-up to the parent so it can `read_subagent`
   later — with `read_subagent(block=true, timeout)` also supported for explicit waits.

---

## 1. How the relevant pieces work today (investigation summary)

### 1.1 The built-in `delegate_task` (what we are *not* reusing wholesale)

`tools/delegate_tool.py` registers `delegate_task` into the `delegation` toolset and spawns
child **`AIAgent`** instances (fresh conversation, restricted toolsets, own `task_id`,
inherited credentials). Key facts that inform our design:

- **Registration shape** (`tools/registry.py:234`):
  `registry.register(name, toolset, schema, handler, check_fn=None, requires_env=None,
  is_async=False, description="", emoji="", max_result_size_chars=None,
  dynamic_schema_overrides=None, override=False)`.
- **Handlers are invoked as `handler(args, **kwargs)`** via `registry.dispatch(name, args,
  **kwargs)` (`tools/registry.py:390`). The model-facing dispatch only forwards
  `task_id=` and `user_task=` (`model_tools.py` `_dispatch`). **Plugin tools do NOT receive
  `parent_agent`.** `delegate_task` only gets `parent_agent` because `AIAgent` special-cases
  it (`run_agent.py:5014 _dispatch_delegate_task(self, …)` passes `parent_agent=self`). Our
  plugin tools must therefore get session/agent context another way (see §6.3).
- **`delegate_task` already supports spawning external ACP subprocesses**: the schema
  carries `acp_command` / `acp_args` (e.g. `copilot --acp --stdio`), and `run_agent.py`
  routes such a child through `agent/copilot_acp_client.py`. **This proves the pattern and
  gives us a reusable ACP-subprocess client** — but note it wires the external agent as the
  *LLM brain of a Hermes loop*, which is **not** what we want; we want the external agent to
  be the *entire* subagent (its own loop + its own tools). We reuse the **JSON-RPC/stdio
  client plumbing**, not the AIAgent-as-shell path.
- **Progress** flows through `parent_agent.tool_progress_callback(event_type, tool_name,
  preview, args, **identity)`; a module-level `_active_subagents` registry +
  `list_active_subagents()` / `interrupt_subagent()` power the TUI `/agents` overlay; plugin
  hooks `subagent_start` / `subagent_stop` fire around each child.
- **Blocked-in-children tools**: `delegate_task`, `clarify`, `memory`, `send_message`,
  `execute_code`. (Irrelevant to external subagents, which don't share Hermes' tool surface,
  but worth mirroring conceptually — our external subagents inherently can't call Hermes
  tools.)

### 1.2 The Hermes plugin API (`hermes_cli/plugins.py` → `PluginContext`)

- `register_tool(name, toolset, schema, handler, check_fn=None, requires_env=None,
  is_async=False, description="", emoji="", override=False)` → delegates to the registry.
  **`is_async=True` is supported**; the executor bridges coroutines via `model_tools._run_async`
  (per-thread persistent loop on worker threads; fresh-thread loop inside the gateway loop).
- `register_hook(name, cb)` with `VALID_HOOKS` including `pre_llm_call` (return
  `{"context": "..."}` to inject into the next turn's user message), `subagent_start`,
  `subagent_stop`, `on_session_start/end`, `pre/post_tool_call`.
- `register_command(name, handler, description, args_hint)` for `/run_subagent` etc.
- `register_skill(name, path)` → namespaced `plugin:skill`.
- `inject_message(content, role="user")` → "If the agent is idle, starts a new turn; if
  running, interrupts and injects." (Our cross-turn wake-up primitive — see §8, with caveats.)
- `ctx.dispatch_tool(name, args, **kwargs)` auto-wires `parent_agent` in CLI mode.
- Discovery: pip entry point `hermes_agent.plugins` (this repo) **or** drop-in
  `~/.hermes/plugins/<name>/` with `plugin.yaml` + `register(ctx)`.

### 1.3 The ACP adapter event flow (`acp_adapter/server.py`, `events.py`)

- ACP `session/prompt` is handled by `HermesACPAgent.prompt(self, …)` (`server.py:1290`).
  It sets streaming callbacks on the agent — `tool_progress_callback`, `reasoning_callback`,
  `step_callback`, `stream_delta_callback` (`server.py:1435-1442`) — then runs the
  **synchronous** agent loop in an executor:
  `ctx = contextvars.copy_context(); await loop.run_in_executor(_executor, ctx.run, _run_agent)`
  (`server.py:1553-1554`).
- Those callbacks turn into ACP `session/update` notifications via
  `events.py::_send_update(conn, session_id, loop, update)`, which crosses the
  thread→loop boundary with `agent.async_utils.safe_schedule_threadsafe(conn.session_update(
  session_id, update), loop)`.
- Tool calls today emit **`tool_call` (pending)** on start (`make_tool_progress_cb`) and
  **`tool_call_update` (completed|failed)** on the next step (`make_step_cb` → `build_tool_complete`).
  **There is no mid-tool-call streaming and no `in_progress` status today.**
- Approvals: `make_approval_callback(conn.request_permission, loop, session_id)` is installed
  into `terminal_tool` **inside `_run_agent`** (executor thread) and bridges
  `session/request_permission` ⇄ Hermes' approval strings.
- **The gap:** there is **no** way for an arbitrary tool to reach `conn` / `loop` to push
  its own `session/update`. `session_id` is reachable (`gateway/session_context.get_session_env
  ("HERMES_SESSION_ID")` + `os.environ`), but `conn` and `loop` are not exposed. Project B
  closes this.
- The `hermes-acp-plugin` patch framework (`patch/__init__.py`) provides idempotent,
  fail-safe `override()` / `wrap(factory)` helpers applied at startup by the
  `hermes-acp-parity` launcher. We add our plumbing as a new patch module there.

### 1.4 External agents — how to drive Devin CLI and Codex

| | **Devin CLI** | **OpenAI Codex** |
|---|---|---|
| Rich/native server | `devin acp` — **native ACP server over stdio** (JSON-RPC) | `codex app-server` — **native JSON-RPC over stdio** (not ACP, but Hermes already has a client) |
| Existing Hermes client to reuse | `agent/copilot_acp_client.py` (generic ACP-subprocess client; talks ACP to any `--acp --stdio` agent) | `agent/transports/codex_app_server.py::CodexAppServerClient` + `codex_app_server_session.py` + `codex_event_projector.py` |
| One-shot fallback | `devin -p --json --model <m> -- "<task>"` | `codex exec --json --model <m> --sandbox <s> "<task>"` |
| Model select | `--model` (e.g. `opus`, `sonnet`, `gpt-5.2`, `codex`) | `--model` (e.g. `gpt-5.5`, `gpt-5.1-codex`) |
| Sandbox / perms | `--sandbox`, `--bypass`/`--yolo` | `--sandbox {read-only,workspace-write,danger-full-access}`, `--ask-for-approval` |
| Resume | `devin -r <session_id>` / `-c` | `codex resume <id>` / `--last`; app-server thread resume |
| Auth (headless) | `WINDSURF_API_KEY`, or `DEVIN_API_KEY`+`DEVIN_ORG_ID`, or `devin auth login` | `OPENAI_API_KEY`, or `$CODEX_HOME/auth.json` |
| Event stream | ACP `session/update` (`tool_call`, `tool_call_update`, `agent_message_chunk`, `plan`, `request_permission`) | `turn/started`, `item/started`, `item/*/delta`, `item/completed`, `turn/completed`, `thread/tokenUsage/updated` |

**Verification items (V-#) collected through the plan are listed in §13.** In particular:
the exact way to pass model/sandbox to `devin acp` (flag vs ACP session-config-option vs env)
is **V-1** and must be confirmed against an installed `devin` before WS3 ships.

---

## 2. Architecture overview

```
                          ┌──────────────────────────────────────────────────────────┐
   ACP client             │  Hermes (acp_adapter)  =  the "parent agent" over ACP      │
   (Devin Desktop / Zed)  │                                                            │
        │  session/prompt  │   AIAgent.run_conversation()  ── calls tool ──▶ run_subagent│
        ▼  ◀ session/update│                                                  │          │
   ┌──────────┐            │      ┌───────────────────────────────────────────┘          │
   │  editor  │            │      ▼                                                       │
   └──────────┘            │  hermes-subagents-overhaul-plugin                            │
        ▲                  │   ├── SubagentManager (registry, fg/bg, resume, cancel)      │
        │  forwarded       │   ├── DevinAcpBackend  ──spawn──▶  `devin acp`  (ACP child)  │
        │  tool_call(_update)/ │   └── CodexAppServerBackend ─spawn▶ `codex app-server`     │
        │  request_permission  │            │  events                                     │
        └───────────────────────────────────┤                                            │
                            │   EventSink (abstract)                                       │
                            │     ├─ ACPEventSink  ── uses ──▶ hermes-acp-plugin runtime    │
                            │     │                            current_acp_session()        │
                            │     └─ ProgressCbSink (CLI/TUI/gateway: tool_progress_callback)│
                            └──────────────────────────────────────────────────────────┘
```

Two halves, mirroring the proven `hermes-acp-plugin` split:

- **Project A (this repo)** is *transport- and platform-agnostic*. It never imports ACP
  types directly; it talks to an abstract `EventSink`. It works on CLI/TUI/gateway (where the
  sink wraps `tool_progress_callback`) and on ACP (where the sink wraps Project B's emitter).
- **Project B (`hermes-acp-plugin`)** owns the ACP protocol surface. It adds a **generic**,
  session-lifetime tool→ACP emitter so this plugin (and any future tool) can stream
  `session/update`s. Project A **soft-imports** Project B; if absent, it degrades to the
  progress-callback sink.

### 2.1 The bridge contract (decoupling the two repos)

- **Project B publishes** a tiny, dependency-free accessor:
  `hermes_acp_plugin.runtime.current_acp_session() -> AcpSessionHandle | None`, where
  `AcpSessionHandle` exposes `start_tool_call(...)`, `update_tool_call(...)`,
  `request_permission(...)`, `plan(...)` — each implemented with
  `safe_schedule_threadsafe(conn.session_update(...)/conn.request_permission(...), loop)`.
  It is backed by a **session-keyed registry** (keyed by ACP `session_id`) holding
  `(conn, loop, request_permission)`, **valid for the entire ACP session lifetime** (not just
  one turn). This is essential because background subagent threads emit events after the
  spawning turn ends.
- **Project A soft-imports it**:
  ```python
  try:
      from hermes_acp_plugin.runtime import current_acp_session
  except Exception:
      current_acp_session = lambda: None
  ```
- **No hard dependency in either direction.** Project A works without Project B (CLI/TUI/gateway).
  Project B's patch only *wires* Project A if present (a `try`-import), exactly like its
  other optional wiring.

> Rationale for putting the emitter in Project B (not A): only Project B legitimately knows
> `conn`/`loop` and owns the ACP adapter patches; making it a **generic** tool emitter is
> independently useful (e.g. long-running `terminal`, future tools) and keeps Project A pure.

---

## 3. Backend abstraction (Project A core)

A small interface every backend implements; the manager treats Devin and Codex uniformly.

```python
# subagents/backends/base.py  (illustrative)
class SubagentEvent(TypedDict):
    kind: str                # "message" | "thought" | "tool_call" | "tool_update"
                             # | "diff" | "permission" | "plan" | "usage" | "done" | "error"
    ...                      # normalized payload (see §7 event mapping)

class SubagentBackend(Protocol):
    name: str                # "devin" | "codex"
    def check_available(self, profile: ResolvedProfile) -> tuple[bool, str]: ...
    def start(self, *, task: str, profile: ResolvedProfile, cwd: str,
              resume_handle: str | None) -> "SubagentHandle": ...

class SubagentHandle(Protocol):
    id: str                  # our agent_id (e.g. "sa_devin_ab12cd")
    backend_session_id: str  # the child's own session/thread id (for resume)
    def events(self) -> Iterator[SubagentEvent]: ...   # blocking stream until done
    def answer_permission(self, request_id: str, outcome: str) -> None: ...
    def cancel(self) -> None: ...
    def result(self) -> SubagentResult: ...            # final summary, status, usage
```

### 3.1 `DevinAcpBackend`

- Spawns `devin acp` (subprocess, stdio). Acts as an **ACP client** to the child, reusing the
  JSON-RPC/stdio framing from `agent/copilot_acp_client.py` (adapt or vendor a thin client; do
  **not** route through `AIAgent`). Sequence: `initialize` → `session/new` (cwd = workspace)
  → `session/prompt` (the `task`); consume `session/update` until the prompt response
  (stop reason); honor `session/request_permission` from the child.
- Maps `ResolvedProfile` → model/sandbox. **V-1:** confirm whether `devin acp` accepts
  `--model`/`--sandbox`, reads `~/.config/devin/config.json`, or expects model via ACP
  session-config-options. Plan supports all three: pass via `acp_args`, env, or a
  `session/set_config_option` after `session/new`.
- Auth: requires `WINDSURF_API_KEY` or `DEVIN_API_KEY`+`DEVIN_ORG_ID`; `check_available`
  verifies the `devin` binary and creds before the tool advertises (`check_fn`).
- Resume: `session/load`/`session/new` with the stored `backend_session_id`, or `devin -r`
  for the one-shot fallback. (Devin resumes run foreground — see §5.)

### 3.2 `CodexAppServerBackend`

- Reuses `agent/transports/codex_app_server.py::CodexAppServerClient` (spawns
  `codex app-server`, JSON-RPC over stdio) and the event shapes already projected by
  `codex_event_projector.py`. Sequence: `initialize` → `thread/start` (cwd, model, sandbox)
  → `turn/start` (the `task`); consume `turn/started` / `item/*` / `item/*/delta` /
  `item/completed` / `turn/completed` / `thread/tokenUsage/updated`.
- Maps `ResolvedProfile` → `--model`, `--sandbox`, `--ask-for-approval`. Approval requests
  surface as Codex items and are forwarded as ACP permission requests for foreground.
- Auth: `OPENAI_API_KEY` or `$CODEX_HOME/auth.json`; `check_available` verifies binary + creds.
- Resume: app-server thread resume / `codex resume <id>` for the stored `backend_session_id`.

### 3.3 Profiles → backend + model + settings

Profiles are config-driven, with built-in defaults mirroring Devin's named profiles. Schema
(under `~/.hermes/config.yaml`):

```yaml
subagents:
  default_backend: codex            # used if a profile omits 'backend'
  workspace: auto                   # 'auto' = parent cwd ($TERMINAL_CWD)
  max_background: 4                 # cap on concurrently-running background subagents
  profiles:
    subagent_explore:               # read-only research
      backend: codex
      model: gpt-5.1-codex
      sandbox: read-only            # enforce no writes
      read_only: true
    subagent_general:               # full access
      backend: codex
      model: gpt-5.5
      sandbox: workspace-write
    coder:
      backend: devin
      model: sonnet
      sandbox: workspace-write
    debugger:
      backend: codex
      model: gpt-5.1-codex
      sandbox: workspace-write
    frontend-developer:
      backend: devin
      model: opus
      sandbox: workspace-write
```

`ResolvedProfile = resolve(profile_name)` merges built-in defaults + user config, validates
the backend is available, and yields `{backend, model, sandbox, read_only, extra_args}`. An
unknown `profile` → tool error listing the available profiles (model-friendly).

> Read-only enforcement (e.g. `subagent_explore`) is delivered by the backend sandbox flag
> (`codex --sandbox read-only`; Devin equivalent is **V-2**), not by Hermes — the external
> agent owns its own tool surface.

---

## 4. The tools (Project A) — schemas identical to Devin's spec

Registered under a dedicated **`subagents`** toolset (single registration; see §6.4 for how it
reaches ACP and CLI). Both are **async handlers** (`is_async=True`).

### 4.1 `run_subagent`

```jsonc
{
  "name": "run_subagent",
  "description": "<<the Devin run_subagent description, adapted: subagents are independent\n   Devin CLI / Codex processes selected by `profile`; foreground blocks and can prompt for\n   approvals, background runs in parallel and notifies on completion>>",
  "parameters": {
    "type": "object",
    "properties": {
      "title":         {"type": "string",  "description": "Short, human-readable title for this subagent."},
      "task":          {"type": "string",  "description": "The full task/prompt. Subagents are stateless: front-load ALL context (paths, names, what you need back)."},
      "profile":       {"type": "string",  "description": "Profile selecting backend+model+permissions. One of: <dynamic list from config>."},
      "is_background": {"type": "boolean", "description": "If true, run in background and return an agent_id immediately; you are notified on completion. Default false.", "default": false},
      "resume":        {"type": "string",  "description": "An agent_id from a previous run_subagent to continue that subagent with this prompt (always runs foreground)."}
    },
    "required": ["title", "task", "profile"]
  }
}
```
`dynamic_schema_overrides` injects the live profile list into the `profile` description
(mirrors `delegate_task`'s dynamic schema), so the model always sees valid choices.

**Return (JSON string):**
- foreground / resume: `{ "agent_id", "status", "summary", "files_changed"?, "usage"?, "backend", "profile" }`
- background: `{ "agent_id", "status": "running", "backend", "profile", "note": "Use read_subagent to collect." }`

### 4.2 `read_subagent`

```jsonc
{
  "name": "read_subagent",
  "parameters": {
    "type": "object",
    "properties": {
      "agent_id": {"type": "string",  "description": "The background subagent's id."},
      "block":    {"type": "boolean", "description": "Block until the subagent finishes or timeout. Default false.", "default": false},
      "timeout":  {"type": "integer", "description": "Max seconds to wait when blocking (0–600). Default 30."}
    },
    "required": ["agent_id"]
  }
}
```
**Return:** the subagent's current state and, if finished, the full result
`{ "agent_id", "status", "summary", "files_changed"?, "usage"?, "exit_reason" }`; if still
running and `block=false`, `{ "agent_id", "status": "running", "elapsed_s", "last_activity" }`.

---

## 5. Subagent lifecycle & manager (Project A)

`SubagentManager` (module-level singleton, like `delegate_tool._active_subagents`) owns:

- **Registry:** `agent_id -> SubagentRecord{ handle, backend, profile, title, status,
  started_at, result, completion_event, sink_ref, fg/bg }`. Survives turns (process-lifetime).
- **Foreground (`is_background=false`):** the tool handler drives `handle.events()`
  **synchronously to completion**, forwarding each event to the sink (§7), and returns the
  final result. Enforce **at most one foreground subagent at a time** (a lock) per Devin.
- **Background (`is_background=true`):** spawn the child, start a **daemon worker thread**
  that drains `handle.events()` → sink and sets `completion_event` + stores `result` when
  done. Return the `agent_id` immediately. The worker holds its own durable `sink_ref`
  (captured at spawn) so it can keep emitting after the turn ends (§2.1 / §8).
- **Resume (`resume=<agent_id>`):** look up the stored `backend_session_id`, start the
  backend in resume mode, **always foreground** (matches Devin; lets the human approve
  previously-denied tools). Resuming a finished agent_id continues its thread/session.
- **Cancel:** `handle.cancel()` (ACP `session/cancel` to the child / kill the app-server turn /
  terminate the subprocess). Wired to parent interruption (§8.3) and a `/subagents` command.
- **Concurrency cap:** `subagents.max_background`; exceeding it → tool error (don't silently
  queue), mirroring `delegate_task`'s batch-limit behavior.
- **Nesting:** external subagents do **not** get `run_subagent` (they have their own tool
  surfaces), so the tree is inherently one level deep — no nesting guard needed. Document it.

---

## 6. Plugin wiring (Project A)

### 6.1 `register(ctx)`

```python
def register(ctx):
    from hermes_subagents_overhaul import contrib
    contrib.run_contributors(ctx)     # auto-discovers contrib/*.py (mirrors acp-plugin)
```
Contributors:
- `contrib/tools.py` → registers `run_subagent` / `read_subagent` (captures `ctx` for
  `inject_message`; see §8).
- `contrib/commands.py` → `/subagents` (list/cancel/foreground) slash command.
- `contrib/hooks.py` → `pre_llm_call` injector for background-completion wake-ups (§8.2);
  `on_session_end` to cancel/cleanup orphaned subagents.

### 6.2 Discovery & manifest

- `pyproject.toml`: `[project.entry-points."hermes_agent.plugins"] hermes-subagents-overhaul =
  "hermes_subagents_overhaul"` (pip path), plus `plugin.yaml` for drop-in installs.
- `requires_env`: none hard (Devin/Codex creds are checked at runtime by `check_fn`, so the
  plugin loads even if only one backend is configured).

### 6.3 Getting session/agent context inside the handler

Plugin tool handlers receive `handler(args, **kwargs)` with `task_id`/`user_task` only — **no
`parent_agent`.** We obtain what we need without it:
- **`session_id`** ← `gateway.session_context.get_session_env("HERMES_SESSION_ID")` (set by
  the ACP adapter inside `_run_agent`; also `os.environ` fallback for CLI/cron).
- **workspace cwd** ← `$TERMINAL_CWD` → else `agent.runtime_cwd` → else `os.getcwd()`
  (same resolution the existing acp-plugin `acp_workspace_info` tool uses).
- **ACP emitter** ← `hermes_acp_plugin.runtime.current_acp_session()` (Project B), keyed by
  that `session_id`.
- **CLI/TUI progress** ← best-effort: if a `parent_agent` ever is available (e.g. via
  `ctx.dispatch_tool`), use its `tool_progress_callback`; otherwise the tool simply returns a
  normal result and the host renders start/complete. (Rich CLI tree-view is a nice-to-have;
  the **required** rich surface is ACP.)

### 6.4 Toolset placement (how the tools reach ACP *and* CLI)

A tool belongs to exactly one toolset. Recommendation:
- Register under **`subagents`**.
- For **ACP**: rely on the acp-plugin's config-driven toolsets (its `patch/toolsets.py`
  honors `acp.enabled_toolsets`). Document `acp.enabled_toolsets: [hermes-acp, subagents]`.
- For **CLI/TUI/gateway**: document enabling the `subagents` toolset (or set it on by default
  via the plugin's own config).
- **Alternative (zero-config ACP):** register under `toolset="hermes-acp"` so the tools appear
  in ACP sessions automatically via `get_toolset()`'s merge (no config needed), and separately
  document CLI enablement. Pick during WS1 based on whether we want ACP-only or both by default.

---

## 7. Event mapping — subagent activity → ACP `session/update`

The `ACPEventSink` translates normalized `SubagentEvent`s into ACP notifications via the
Project B emitter. **Each subagent owns a stable umbrella `toolCallId`** = its `agent_id`.

### 7.1 Background subagent (collapsed, streaming)

| Subagent event | ACP emission |
|---|---|
| spawn | `tool_call` { toolCallId=agent_id, title=`"<profile> subagent: <title>"`, kind=`other` (or `think`), status=`in_progress`, rawInput={task,…} } |
| message/thought/tool/tool_update/diff (throttled) | `tool_call_update` { toolCallId=agent_id, status=`in_progress`, content=[text block with the latest activity line / running summary] } |
| usage | optional `tool_call_update` content note (tokens), or none |
| done | `tool_call_update` { status=`completed`/`failed`, content=[final summary], rawOutput=result } |

This realizes Devin's "you do not see the subagent's raw output directly… the parent
summarizes." Throttle/batch updates (e.g. coalesce every N events or ~1s) to avoid flooding —
mirror `delegate_task`'s batch-of-5 relay.

### 7.2 Foreground subagent (rich forwarding)

Still emit the umbrella `tool_call` (so there's a clear parent node), **plus** forward the
child's granular activity:

| Subagent event | ACP emission into parent session |
|---|---|
| child `tool_call` / `tool_call_update` (Devin) or `item/*` (Codex) | parent `tool_call` / `tool_call_update` with **namespaced** `toolCallId` = `"<agent_id>:<child_id>"`, preserving `kind`, `status`, `content`, `locations` |
| diffs | forwarded as `tool_call_update` content `{type:"diff", path, oldText, newText}` |
| `agent_message_chunk` / reasoning | parent `agent_message_chunk` / `agent_thought_chunk` (prefixed with the subagent title) **or** umbrella `tool_call_update` content |
| **child `request_permission`** | **call parent `session/request_permission`** (via the Project B emitter), then relay the chosen outcome back to the child. This is how "foreground subagents prompt for approval" works end-to-end. |
| done | umbrella `tool_call_update` { status=`completed`/`failed`, summary } |

ACP has no native tool-call nesting/grouping, so hierarchy is conveyed via `toolCallId`
namespacing + descriptive `title`s (and optionally `_meta.hermes.subagentId` for clients that
want to group). Document this explicitly.

### 7.3 Optional: ACP plan surfacing

If a backend exposes a plan/todo (Codex reasoning items; Devin plans), optionally mirror it as
an ACP `plan` update (full entry list each time, per spec). Low priority; behind a config flag.

---

## 8. Cross-turn background & wake-up (the trickiest part)

Background subagents must outlive the spawning turn and "notify" the parent on completion,
Devin-style. Three layered mechanisms, in priority order:

### 8.1 Durable emitter (already designed)

The background worker captures a **durable** `AcpSessionHandle` at spawn (Project B's
registry is session-lifetime, not turn-lifetime), so it can emit the final
`tool_call_update(completed)` even minutes later, as long as the ACP session is alive. (If the
session has ended, emission is a safe no-op.)

### 8.2 Wake-up to the parent model (ACP-native, reliable)

On completion, the worker stores the result and **flags a pending notification** in the
manager. A `pre_llm_call` hook (registered by Project A) drains pending notifications and
returns `{"context": "<subagent_completion_notification>agent_id=… status=… title=…\n"
"Call read_subagent(agent_id) to collect the full result.</subagent_completion_notification>"}`
so the **next** time the parent model runs, it's told to collect results. This requires **no
server-initiated turns** and works on every platform.

### 8.3 Proactive wake (best-effort, platform-dependent)

Additionally call `ctx.inject_message(notification, role="user")` to *proactively* prompt an
idle parent. **V-3:** confirm `inject_message` behavior under the ACP adapter — whether it can
start a server-initiated turn or only takes effect on the next client prompt. If ACP can't
self-initiate a turn, §8.2 still guarantees delivery on the next user turn, and the editor has
already *seen* completion via §8.1's `tool_call_update`. Document this honestly: **guaranteed
visibility (8.1) + guaranteed model-delivery on next turn (8.2) + best-effort auto-wake (8.3).**

### 8.4 Interrupt propagation

On parent interruption/cancel: Project A registers cleanup. For ACP, Project B optionally wraps
the `session/cancel` handler to call `SubagentManager.cancel_all(session_id)`; `on_session_end`
cancels any survivors. `read_subagent(block=true)` respects the same cancel + its own timeout.

---

## 9. Project B changes (`hermes-acp-plugin`)

Add **one new module** and **one new patch**, following its existing conventions
(`patch.wrap`/`override`, idempotent, fail-safe, stderr-only logging, version-guarded). No
edits to existing patch files; method-ownership respected.

### 9.1 `hermes_acp_plugin/runtime.py` (new, generic)

- A process-global **session registry**: `_sessions: dict[str, _Entry]` guarded by a lock,
  where `_Entry = (conn, loop, request_permission)`.
- `register_session(session_id, conn, loop, request_permission)` / `unregister_session(session_id)`.
- `current_acp_session() -> AcpSessionHandle | None`: resolves `session_id` via
  `gateway.session_context.get_session_env("HERMES_SESSION_ID")`, looks it up, returns a handle.
- `AcpSessionHandle` methods (all thread-safe, fire-and-forget via
  `agent.async_utils.safe_schedule_threadsafe(...)`, mirroring `events.py::_send_update`):
  - `start_tool_call(tool_call_id, title, kind="other", status="in_progress", **fields)`
  - `update_tool_call(tool_call_id, status=None, content=None, raw_output=None, **fields)`
  - `request_permission(tool_call, options) -> outcome` (blocking with timeout; for foreground
    permission forwarding)
  - `plan(entries)`
  - builds blocks via the installed `acp` SDK helpers (`acp.start_tool_call`,
    `acp.update_tool_call`, `acp.tool_content`, `acp.text_block`, diff blocks).

### 9.2 `hermes_acp_plugin/patch/tool_session_access.py` (new patch)

- `apply(ctx)` wraps **`HermesACPAgent.prompt`** with `patch.wrap` (chaining-safe). The
  wrapper, at entry, calls `runtime.register_session(session_id, self._conn,
  asyncio.get_running_loop(), self._conn.request_permission)`; in `finally`, it **does not**
  unregister on every turn (the session outlives turns) — instead register is **idempotent**
  and refreshes `(conn, loop)`; unregistration is wired to session teardown.
- Also wrap a session-teardown method (e.g. the `session/cancel` path and/or the
  `SessionManager` close) to `unregister_session` + `SubagentManager.cancel_all` (soft-import
  Project A). **V-4:** identify the precise teardown hook (`server.py` close / `session_manager`
  removal); if none is clean, fall back to unregister on a session-not-found prompt + a TTL.
- **Soft-wire Project A** (optional): nothing required — Project A pulls from
  `runtime.current_acp_session()`. The only Project B → Project A coupling is the optional
  cancel-on-teardown `try`-import.
- Idempotent, version-guarded; if `prompt`/`_conn` shapes differ on a future Hermes, log to
  stderr and no-op (degrade to today's behavior).

### 9.3 Capability touch-ups (reuse existing WS1)

Foreground permission forwarding relies on `session/request_permission`, already used by the
adapter. No initialize-capability change is required for subagents specifically (we emit
standard `tool_call`/`tool_call_update`, which need no capability gating). Confirm the editor
renders forwarded permission prompts (Devin Desktop ignores session modes but honors
`request_permission`).

---

## 10. Proposed repo layout (Project A)

```
hermes-subagents-overhaul-plugin/
├── PLAN.md                      # this file
├── README.md                    # install + config + parity notes
├── pyproject.toml               # package: hermes_subagents_overhaul; plugin entry point
├── plugin.yaml                  # drop-in manifest
├── hermes_subagents_overhaul/
│   ├── __init__.py              # register(ctx)
│   ├── config.py                # profiles resolution + defaults
│   ├── manager.py               # SubagentManager (registry, fg/bg/resume/cancel)
│   ├── tools_schema.py          # run_subagent / read_subagent schemas (+ dynamic overrides)
│   ├── sinks/
│   │   ├── base.py              # EventSink protocol + normalized SubagentEvent
│   │   ├── acp_sink.py          # soft-imports hermes_acp_plugin.runtime
│   │   └── progress_sink.py     # tool_progress_callback fallback (CLI/TUI/gateway)
│   ├── backends/
│   │   ├── base.py              # SubagentBackend / SubagentHandle protocols
│   │   ├── devin_acp.py         # spawn `devin acp`; ACP client (reuse copilot_acp_client patterns)
│   │   └── codex_app_server.py  # reuse CodexAppServerClient
│   └── contrib/
│       ├── __init__.py          # run_contributors() (mirror acp-plugin)
│       ├── tools.py             # register_tool(run_subagent/read_subagent), captures ctx
│       ├── commands.py          # /subagents
│       └── hooks.py             # pre_llm_call wake-up + on_session_end cleanup
├── scripts/
│   ├── install.py / uninstall.py
│   └── doctor.py                # check devin/codex binaries+creds, acp-plugin presence, bridge
└── tests/
    ├── conftest.py              # fakes for backends + a fake ACP emitter
    ├── test_profiles.py
    ├── test_manager_fg_bg_resume.py
    ├── test_event_mapping.py    # SubagentEvent -> emitter calls (fg rich / bg collapsed)
    ├── test_wakeup.py           # pre_llm_call injection + read_subagent(block)
    └── test_backends_*.py       # against mock devin-acp / codex-app-server processes
```

---

## 11. Workstreams (independently shippable)

- **WS0 — Scaffold & harness.** Package, `plugin.yaml`, `register(ctx)`, contrib auto-discovery,
  `doctor.py`. Fake backend + fake ACP emitter test fixtures. *Accept:* plugin loads; tools
  appear in a fake session; doctor green.
- **WS1 — Tools + SubagentManager (foreground, one backend).** `run_subagent` (fg only) with
  `CodexAppServerBackend` (reuses existing Hermes client → least new code). Profiles config.
  *Accept:* a foreground Codex subagent runs end-to-end in CLI and returns a summary.
- **WS2 — Project B bridge.** `runtime.py` + `patch/tool_session_access.py` in the acp-plugin;
  `acp_sink.py` in Project A. *Accept:* a foreground subagent's umbrella `tool_call` +
  `tool_call_update(completed)` show up in an ACP conformance test (extend acp-plugin's
  harness).
- **WS3 — DevinAcpBackend.** Spawn `devin acp`; ACP client; resolve V-1/V-2. *Accept:* a
  foreground Devin subagent runs and surfaces events over ACP.
- **WS4 — Rich foreground forwarding + permission relay.** Namespaced child tool-calls/diffs +
  `request_permission` round-trip. *Accept:* approving a subagent's command in the editor
  unblocks the child; diffs render.
- **WS5 — Background + cross-turn wake-up.** Background workers, durable emitter,
  `read_subagent(block)`, `pre_llm_call` notification, best-effort `inject_message`,
  cancel/cleanup. *Accept:* background subagent completes after the spawning turn; parent is
  told and collects via `read_subagent`; editor shows completion.
- **WS6 — Resume + `/subagents` + polish.** Resume (foreground), slash command, throttling,
  usage reporting, docs. *Accept:* resume continues a prior subagent; `/subagents` lists/cancels.

Each WS ships with focused tests run via the repo's venv (mirror acp-plugin's
`./.venv/bin/python -m pytest tests/test_<ws>.py -q`).

---

## 12. Configuration (summary)

```yaml
# ~/.hermes/config.yaml
subagents:
  default_backend: codex
  workspace: auto
  max_background: 4
  notify_via_inject: true          # 8.3 best-effort proactive wake
  profiles: { … see §3.3 … }

acp:
  enabled_toolsets: [hermes-acp, subagents]   # so ACP sessions expose the new tools
```
Env/creds: Devin (`WINDSURF_API_KEY` **or** `DEVIN_API_KEY`+`DEVIN_ORG_ID`), Codex
(`OPENAI_API_KEY` **or** `$CODEX_HOME/auth.json`). `check_fn` gates each tool/profile on the
relevant binary + creds being present.

---

## 13. Verification items (resolve against installed tooling before the dependent WS)

- **V-1 (WS3):** How `devin acp` accepts model/sandbox — CLI flag, `~/.config/devin/config.json`,
  or ACP `session/set_config_option`. Support whichever is real.
- **V-2 (WS3):** Devin's read-only/sandbox posture equivalent to Codex `--sandbox read-only`
  (for `subagent_explore`).
- **V-3 (WS5):** `ctx.inject_message` behavior under the ACP adapter (can it start a turn, or
  only affect the next client prompt?). Confirm the layered fallback (§8.2) is sufficient.
- **V-4 (WS2):** The exact ACP session-teardown hook for `unregister_session` /
  `cancel_all` (and whether `prompt`/`self._conn` shapes match the pinned Hermes version).
- **V-5 (WS1):** `CodexAppServerClient` constructor/args for model+sandbox+cwd and its event
  field names (re-read `codex_app_server.py` / `codex_event_projector.py` against the installed
  `codex`; the app-server protocol moves fast — pin a tested `codex` version).
- **V-6 (WS3):** Reusing `copilot_acp_client.py` vs vendoring a minimal ACP client — confirm it
  can be driven as a *pure* ACP client (not as an AIAgent transport) and handles
  `session/request_permission` inbound.
- **V-7 (WS5):** Whether `safe_schedule_threadsafe` from a long-lived background thread to the
  session's `loop` remains valid for the whole session (it should — single server loop).

---

## 14. Risks & mitigations

- **External-process flakiness / version drift.** Devin/Codex CLIs evolve. *Mitigation:*
  backends isolated behind `SubagentBackend`; `doctor.py` + `check_fn` gate on detected
  versions; one-shot (`-p --json` / `exec --json`) fallbacks per backend if the rich server is
  unavailable.
- **ACP has no native nested tool-calls.** *Mitigation:* namespaced `toolCallId` + titles +
  `_meta` hints; documented.
- **No server-initiated turns in ACP.** *Mitigation:* §8 layered wake-up (visibility via
  `tool_call_update`, delivery via `pre_llm_call`, best-effort `inject_message`).
- **Auth in headless/editor contexts.** *Mitigation:* `check_fn` advertises a backend only when
  usable; clear doctor diagnostics; per-profile backend selection lets users run only the
  backend they've authed.
- **Cost/runaway parallelism.** *Mitigation:* `max_background` cap; cancel on parent
  interrupt/session end; one foreground at a time.
- **Coupling between repos.** *Mitigation:* soft `try`-imports both ways; each repo fully
  functional alone (Project A degrades to progress-callback sink; Project B's patch no-ops if
  Project A absent).

## 15. Non-goals (this iteration)

- Replacing or modifying `delegate_task` (kept as-is per decision 1).
- Editing the installed Hermes core directly (all core-surface changes go through the
  reversible acp-plugin patch framework).
- Nested external subagents (external agents don't get `run_subagent`).
- Non-ACP editors' rich UIs beyond what `tool_progress_callback` already gives the TUI.
```
