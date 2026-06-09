"""``/subagents`` slash command: list and cancel subagents."""

from __future__ import annotations

import logging
from typing import Any

from hermes_subagents_overhaul.manager import get_manager

logger = logging.getLogger("hermes_subagents_overhaul.contrib.commands")


def _format_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No subagents this session."
    out = ["Subagents:"]
    for r in rows:
        bg = "bg" if r.get("background") else "fg"
        line = (
            f"  {r['agent_id']}  [{bg}] {r['status']}  {r['backend']}/{r['profile']}  "
            f"{r['elapsed_s']}s  {r.get('title','')}"
        )
        out.append(line)
        if r.get("last_activity"):
            out.append(f"      last: {r['last_activity']}")
    return "\n".join(out)


def _handle_subagents(arg: str = "", **kwargs: Any) -> str:
    mgr = get_manager()
    arg = (arg or "").strip()
    if arg.startswith("cancel"):
        parts = arg.split()
        if len(parts) >= 2:
            ok = mgr.cancel(parts[1])
            return f"Cancelled {parts[1]}." if ok else f"Could not cancel {parts[1]}."
        n = mgr.cancel_all()
        return f"Cancelled {n} running subagent(s)."
    return _format_list(mgr.list())


def contribute(ctx: Any) -> None:
    register = getattr(ctx, "register_command", None)
    if not callable(register):
        return
    try:
        register(
            "subagents",
            _handle_subagents,
            "List running/finished subagents; '/subagents cancel [agent_id]' to cancel.",
            "[cancel [agent_id]]",
        )
    except Exception as exc:  # pragma: no cover - older Hermes / gateway-only
        logger.debug("register_command('subagents') failed: %s", exc)
