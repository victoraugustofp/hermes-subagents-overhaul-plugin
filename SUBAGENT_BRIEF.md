# Subagent Brief — hermes-subagents-overhaul-plugin

You are implementing ONE workstream of this plugin. Read this brief, then `PLAN.md`,
then the research note(s) for your workstream under `docs/research/`. Validate every
protocol claim against the official docs (links below) and the real Hermes 0.16.0
source under `../hermes-acp-plugin/.reference/hermes-agent/` (the exact code in the
shared dev venv).

Repo root (Project A): `/Users/victor/Documents/GitHub/hermes-subagents-overhaul-plugin`
Sibling repo (Project B): `/Users/victor/Documents/GitHub/hermes-acp-plugin`

## Goal & decisions (LOCKED — do not relitigate)
1. Coexist with `delegate_task`. We add NEW `run_subagent` / `read_subagent` tools.
2. `profile` selects backend (`devin`|`codex`) + model + sandbox/permission. No
   separate `backend` arg on the tool.
3. Rich native transport: `devin acp` (ACP) and `codex app-server` (JSON-RPC).
   Foreground forwards granular tool-calls/diffs/permissions; background collapses to a
   single streaming `tool_call` + final summary.
4. True cross-turn background: background subagents outlive the turn; notify the parent
   via the `pre_llm_call` hook + a durable ACP emitter; `read_subagent(block=true)` waits.

## Dev environment (already set up — DO NOT reinstall / no `pip install` needed)
- Shared venv: `/Users/victor/Documents/GitHub/hermes-acp-plugin/.venv` — has
  `hermes-agent==0.16.0` (editable), `acp==0.9.0`, `pytest`, `pytest-asyncio`,
  `hermes_acp_plugin` (editable), and THIS package (editable).
- Run YOUR tests only:
  `cd <repo> && /Users/victor/Documents/GitHub/hermes-acp-plugin/.venv/bin/python -m pytest tests/test_<yourfile>.py -q`
- `tests/conftest.py` isolates `HERMES_HOME` to a temp dir automatically.
- Both real CLIs are installed and authenticated: `devin` (Enterprise),
  `codex` (`OPENAI_API_KEY` + `~/.codex/auth.json`). Real end-to-end runs are EXPECTED
  for acceptance — keep real tasks trivially cheap (e.g. "reply with the word DONE").
- Do NOT modify the venv, do NOT `git commit` (the orchestrator commits per workstream).

## Foundation contract (FROZEN — read, do not edit these shared files)
These define the interfaces every workstream plugs into. Treat as read-only:
- `hermes_subagents_overhaul/__init__.py` (`register`), `contrib/__init__.py`
  (auto-discovery), `config.py` (`ResolvedProfile`, `resolve_profile`, profiles),
  `backends/base.py` (`SubagentEvent`, `SubagentResult`, `SubagentBackend`,
  `SubagentHandle`, `event()`, `new_agent_id()`), `sinks/base.py` (`SubagentSink`,
  `NullSink`, `make_sink`, outcomes), `manager.py` (`SubagentManager`, `get_manager`),
  `tools_schema.py`, `contrib/tools.py`, `contrib/hooks.py`, `contrib/commands.py`,
  `doctor.py`, `tests/conftest.py`, `pyproject.toml`, `plugin.yaml`, `PLAN.md`,
  anything in `docs/research/` or `.reference/`.
- If you genuinely need a change to a frozen file, STOP and report it — don't edit.

## Normalized event contract (how backends talk to sinks)
Backends translate native events into `SubagentEvent` dicts (`backends/base.py`):
`kind ∈ {message, thought, tool_call, tool_update, diff, permission, plan, usage,
status, error}`. The manager drains `handle.events()` and forwards to the sink. For
approvals, yield `kind="permission"` with `permission={request_id, title, options,
tool_call}` and BLOCK until the manager calls `handle.answer_permission(request_id,
outcome, option_id)`. `result()` returns a `SubagentResult` once `events()` ends.

## Method / file ownership (avoid collisions — one owner each)
| WS | Owns (this repo unless noted) | Notes |
|----|------------------------------|-------|
| WS1 Codex backend | `backends/codex_app_server.py` + `tests/test_backend_codex.py` | reuse `CodexAppServerSession`; see docs/research/codex-app-server.md |
| WS3 Devin backend | `backends/devin_acp.py` + `tests/test_backend_devin.py` | vendor sync ACP client; see docs/research/devin-acp-client.md |
| Sinks | `sinks/acp_sink.py` + `sinks/progress_sink.py` + `tests/test_event_mapping.py` | implement PLAN §7; keep `try_make_*` signatures |
| WS2 bridge (Project B) | `hermes_acp_plugin/runtime.py` + `hermes_acp_plugin/patch/tool_session_access.py` + `tests/test_tool_session_access.py` (in the acp-plugin repo) | see docs/research/acp-bridge.md |
| WS6 polish | `README.md`, `docs/*` (not research), extra `/subagents` polish behind flags | do not change frozen APIs |

Do NOT edit another workstream's files. Keep the `try_make_acp_sink` /
`try_make_progress_sink` signatures exactly as defined in `sinks/base.py`.

## Official docs (source of truth — validate as you go)
- ACP: https://agentclientprotocol.com/protocol/v1/{overview,prompt-turn,content,session-setup,extensibility}
- ACP Python SDK: https://agentclientprotocol.github.io/python-sdk/
- Devin Desktop custom ACP agent: https://docs.devin.ai/desktop/acp-custom ; Devin CLI: https://docs.devin.ai
- OpenAI Codex: https://github.com/openai/codex
- Hermes plugins / ACP internals: https://hermes-agent.nousresearch.com/docs/developer-guide/acp-internals ,
  https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins

## Coding standards
Match Hermes/acp-plugin style: small, idiomatic, defensive try/except only at real
boundaries (subprocess, JSON-RPC, threads). Sinks and backends must NEVER raise into
the manager. Write focused tests; run them; report pass/fail honestly. No new hard deps.
