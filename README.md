# hermes-subagents-overhaul-plugin

A Hermes plugin that gives Hermes a **Devin-CLI-style subagent system** — `run_subagent` /
`read_subagent`, **foreground and background** — where the subagents are **real external AI
agents** (**Devin CLI** via `devin acp`, **OpenAI Codex** via `codex app-server`), **not**
in-process Hermes `AIAgent` children like the built-in `delegate_task`.

Subagents are spawned **via tools** (never by the model shelling out), so their lifecycle and
activity surface as **proper ACP events** — for both foreground and background subagents —
through the companion [`hermes-acp-plugin`](../hermes-acp-plugin).

See **[`PLAN.md`](./PLAN.md)** for the full end-to-end architecture and build spec.

## At a glance

- **Tools (Devin-spec schemas):** `run_subagent(title, task, profile, is_background, resume)`
  and `read_subagent(agent_id, block, timeout)`.
- **Profiles → backend + model + settings.** `profile` selects Devin vs Codex, the model, and
  the sandbox/permission posture (config-driven; defaults mirror Devin's named profiles).
- **Rich native transport.** Foreground subagents forward their granular tool-calls, diffs,
  and permission prompts into the parent ACP session; background subagents collapse to a single
  streaming `tool_call` plus a final summary.
- **True cross-turn background.** Background subagents outlive the spawning turn and notify the
  parent on completion; `read_subagent(block=true)` also supports explicit waits.
- **Two coordinated repos.** This plugin is platform-agnostic and talks to an abstract event
  sink; the sibling `hermes-acp-plugin` adds a generic, session-lifetime tool→ACP emitter so
  the activity becomes `session/update` notifications. Each repo works without the other
  (graceful degradation).

## Status

Design complete (`PLAN.md`); implementation tracked as workstreams WS0–WS6 in the plan.

## License

MIT
