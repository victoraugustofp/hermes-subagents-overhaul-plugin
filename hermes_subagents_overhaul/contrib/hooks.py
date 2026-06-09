"""Hooks: background-completion wake-up (``pre_llm_call``) and session cleanup.

* ``pre_llm_call`` drains pending background-completion notifications and injects a
  ``<subagent_completion_notification>`` block into the next turn's user message, so
  the parent model is reliably told to ``read_subagent`` (ACP-native; needs no
  server-initiated turn). See PLAN.md §8.2.
* ``on_session_end`` cancels any survivors (CLI/TUI/gateway). Under the ACP adapter
  this hook may not fire — the companion ``hermes-acp-plugin`` teardown patch handles
  ACP cancellation instead.
"""

from __future__ import annotations

import logging
from typing import Any

from hermes_subagents_overhaul.manager import get_manager

logger = logging.getLogger("hermes_subagents_overhaul.contrib.hooks")


def _format_notification(notes: list[dict[str, Any]]) -> str:
    lines = ["<subagent_completion_notification>"]
    for n in notes:
        lines.append(
            f"agent_id={n['agent_id']} status={n.get('status')} "
            f"profile={n.get('profile')} title={n.get('title')!r}"
        )
    lines.append(
        "One or more background subagents finished. Call read_subagent(agent_id) "
        "to collect each full result."
    )
    lines.append("</subagent_completion_notification>")
    return "\n".join(lines)


def _pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
    try:
        notes = get_manager().drain_notifications()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("drain_notifications failed: %s", exc)
        return None
    if not notes:
        return None
    return {"context": _format_notification(notes)}


def _on_session_end(**kwargs: Any) -> None:
    try:
        get_manager().cancel_all()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("on_session_end cancel_all failed: %s", exc)


def contribute(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
