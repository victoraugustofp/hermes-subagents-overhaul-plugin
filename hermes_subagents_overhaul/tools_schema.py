"""JSON schemas for ``run_subagent`` / ``read_subagent`` — identical in shape to
Devin CLI's published spec (the ``profile`` enum is populated live from config).
"""

from __future__ import annotations

from typing import Any

from hermes_subagents_overhaul import config

RUN_SUBAGENT_DESCRIPTION = (
    "Launch an independent subagent to handle a task autonomously. Subagents are "
    "real external AI-agent processes (Devin CLI or OpenAI Codex), selected by "
    "`profile`. Use them for self-contained, multi-step work or to parallelize.\n\n"
    "- Foreground (default): blocks until the subagent finishes and returns its "
    "summary; it can prompt you for approvals.\n"
    "- Background (`is_background=true`): returns an `agent_id` immediately and runs "
    "in parallel; you are notified on completion and collect the result with "
    "`read_subagent`.\n\n"
    "Subagents are stateless: front-load ALL context (file paths, names, exactly "
    "what you need back) into `task`. Subagents cannot launch their own subagents."
)

READ_SUBAGENT_DESCRIPTION = (
    "Read the result/status of a subagent started by `run_subagent` (typically a "
    "background one). With `block=true`, wait until it finishes or `timeout` "
    "seconds elapse."
)


def _profiles_help(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg if cfg is not None else config.load_config()
    parts = []
    for name in config.available_profiles(cfg):
        spec = cfg["profiles"][name]
        desc = spec.get("description") or f"{spec.get('backend')} / {spec.get('model')}"
        parts.append(f"{name} ({desc})")
    return "; ".join(parts)


def run_subagent_schema(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else config.load_config()
    profiles = config.available_profiles(cfg)
    return {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short, human-readable title for this subagent.",
            },
            "task": {
                "type": "string",
                "description": (
                    "The full task/prompt. Subagents are stateless: front-load ALL "
                    "context (paths, names, what you need back)."
                ),
            },
            "profile": {
                "type": "string",
                "enum": profiles,
                "description": "Profile selecting backend+model+permissions. " + _profiles_help(cfg),
            },
            "is_background": {
                "type": "boolean",
                "description": (
                    "If true, run in background and return an agent_id immediately; "
                    "you are notified on completion. Default false."
                ),
                "default": False,
            },
            "resume": {
                "type": "string",
                "description": (
                    "An agent_id from a previous run_subagent to continue that "
                    "subagent with this prompt (always runs foreground)."
                ),
            },
        },
        "required": ["title", "task", "profile"],
    }


def read_subagent_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The subagent's id."},
            "block": {
                "type": "boolean",
                "description": "Block until the subagent finishes or timeout. Default false.",
                "default": False,
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait when blocking (0-600). Default 30.",
                "default": 30,
            },
        },
        "required": ["agent_id"],
    }
