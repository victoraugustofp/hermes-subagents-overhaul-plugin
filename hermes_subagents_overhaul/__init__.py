"""hermes-subagents-overhaul — Hermes plugin entry point.

Gives Hermes a Devin-CLI-style subagent system: ``run_subagent`` / ``read_subagent``,
foreground and background, backed by real external AI-agent processes (Devin CLI via
``devin acp``; OpenAI Codex via ``codex app-server``).

Hermes discovers this package (drop-in dir or the ``hermes_agent.plugins`` pip entry
point) and calls :func:`register`, which auto-runs every contributor module in
``hermes_subagents_overhaul.contrib`` — so adding a feature means adding a file, never
editing this one. See PLAN.md for the full architecture.
"""

from __future__ import annotations

__version__ = "0.1.0"


def register(ctx) -> None:  # noqa: ANN001 - ctx is Hermes' PluginContext
    """Hermes plugin hook. Runs all contributors in ``hermes_subagents_overhaul.contrib``."""
    from hermes_subagents_overhaul import contrib

    contrib.run_contributors(ctx)
