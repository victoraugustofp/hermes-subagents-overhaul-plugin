# hermes-subagents-overhaul-plugin

A Hermes plugin that gives Hermes a **Devin-CLI-style subagent system** — `run_subagent` /
`read_subagent`, **foreground and background** — where the subagents are **real external AI
agents** (**Devin CLI** via `devin acp`, **OpenAI Codex** via `codex app-server`), **not**
in-process Hermes `AIAgent` children like the built-in `delegate_task`.

Subagents are spawned **via tools** (never by the model shelling out), so their lifecycle and
activity surface as **proper ACP events** — for both foreground and background subagents —
through the companion [`hermes-acp-plugin`](../hermes-acp-plugin).

See **[`PLAN.md`](./PLAN.md)** for the full architecture and **[`docs/research/`](./docs/research)**
for the validated transport/bridge notes.

## At a glance

- **Tools (Devin-spec schemas):** `run_subagent(title, task, profile, is_background, resume)`
  and `read_subagent(agent_id, block, timeout)`.
- **Profiles → backend + model + settings.** `profile` selects Devin vs Codex, the model, and
  the sandbox/permission posture (config-driven; defaults mirror Devin's named profiles).
- **Rich native transport.** Foreground subagents forward their granular tool-calls, diffs,
  and permission prompts into the parent ACP session; background subagents collapse to a single
  streaming `tool_call` plus a final summary (throttled).
- **True cross-turn background.** Background subagents outlive the spawning turn and notify the
  parent on completion via a `pre_llm_call` wake-up + a durable ACP emitter; `read_subagent(block=true)`
  also supports explicit waits.
- **Two coordinated repos.** This plugin is platform-agnostic and talks to an abstract event
  sink; the sibling `hermes-acp-plugin` adds a generic, session-lifetime tool→ACP emitter so the
  activity becomes `session/update` notifications. Each repo works without the other (graceful
  degradation to the CLI/TUI progress callback).

## Install

Install into the same environment as your `hermes-agent`:

```bash
/path/to/hermes-venv/bin/python scripts/install.py            # or: pip install .
/path/to/hermes-venv/bin/python scripts/install.py --editable # dev mode
hermes-subagents-doctor                                       # check backends/creds/bridge
```

## Enable

Entry-point plugins are **opt-in** in Hermes. Enable it and (for ACP) expose the toolset:

```bash
hermes plugins enable hermes-subagents-overhaul
```

```yaml
# ~/.hermes/config.yaml
plugins:
  enabled: [hermes-subagents-overhaul]      # CLI/TUI/gateway

acp:
  enabled_toolsets: [hermes-acp, subagents] # so ACP sessions expose the tools
```

## Configure profiles

```yaml
# ~/.hermes/config.yaml
subagents:
  default_backend: codex
  workspace: auto            # 'auto' -> parent cwd ($TERMINAL_CWD)
  max_background: 4
  throttle_seconds: 1.0
  profiles:
    subagent_explore:   { backend: codex, model: gpt-5.1-codex, sandbox: read-only, read_only: true }
    subagent_general:   { backend: codex, model: gpt-5.5,       sandbox: workspace-write }
    coder:              { backend: devin, model: sonnet,        sandbox: workspace-write }
    debugger:           { backend: codex, model: gpt-5.1-codex, sandbox: workspace-write }
    frontend-developer: { backend: devin, model: opus,          sandbox: workspace-write }
```

**Credentials.** Codex: `OPENAI_API_KEY` or `$CODEX_HOME/auth.json`. Devin (ACP is
sole-credential — the host authenticates): `WINDSURF_API_KEY`, or `windsurf_api_key` from
`devin auth login`. `hermes-subagents-doctor` reports what's usable; each backend's `check_fn`
gates the tool so it only advertises when a backend is ready.

## Usage

```jsonc
// foreground: blocks, can prompt for approvals, returns a summary
run_subagent({ "title": "audit", "task": "Find all TODOs under src/ and summarize", "profile": "subagent_explore" })

// background: returns immediately; collect later
run_subagent({ "title": "tests", "task": "Run the test suite and report failures",
               "profile": "subagent_general", "is_background": true })
read_subagent({ "agent_id": "sa_codex_ab12cd", "block": true, "timeout": 120 })

// resume a prior subagent with more context (always foreground)
run_subagent({ "title": "fix", "task": "Now fix the first failure", "profile": "subagent_general",
               "resume": "sa_codex_ab12cd" })
```

`/subagents` lists running/finished subagents; `/subagents cancel [agent_id]` cancels.

## Testing

```bash
# unit (deterministic, no external processes):
.venv/bin/python -m pytest tests -q

# real end-to-end against the actual codex/devin binaries (costs quota; needs auth):
HSO_RUN_REAL=1 .venv/bin/python -m pytest tests/test_integration_real.py -q -s
```

## Status

Implemented and verified end-to-end: Codex (foreground / background / resume) and Devin
(foreground / background) run against the real binaries; the ACP bridge surfaces subagent
activity as `session/update` notifications. Workstreams WS0–WS6 (see `PLAN.md`) are complete.

## License

MIT
