"""CLI/TUI/gateway event sink — bridges subagent activity to a Hermes
``tool_progress_callback`` (the same callback ``delegate_task`` uses).

Implements :class:`ProgressSubagentSink` per PLAN.md §7, forwarding start/event/
done as ``tool_progress_callback(event_type, tool_name, preview, args, **identity)``
calls (throttled for background) and returning a non-interactive
:data:`~hermes_subagents_overhaul.sinks.base.OUTCOME_DENY` from ``request_permission``
when no approver is wired.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from hermes_subagents_overhaul.backends.base import SubagentEvent, SubagentResult
from hermes_subagents_overhaul.sinks.base import OUTCOME_DENY, SubagentSink


class ProgressSubagentSink:
    """Bridges subagent activity to a Hermes tool_progress_callback."""

    def __init__(
        self,
        agent_id: str,
        progress_cb: Callable,
        background: bool = False,
    ) -> None:
        """Initialize the progress sink.

        Args:
            agent_id: The subagent's id
            progress_cb: Hermes tool_progress_callback(event_type, tool_name, preview, args, **identity)
            background: If True, throttle updates; if False, forward all
        """
        self.agent_id = agent_id
        self.progress_cb = progress_cb
        self.background = background

        # For background mode: track latest activity and throttle updates
        self._last_update_time = 0.0
        self._throttle_seconds = 1.0

    def start(
        self,
        *,
        title: str,
        profile: str,
        backend: str,
        task: str,
        model: str | None = None,
    ) -> None:
        """Emit a tool_start event."""
        try:
            self.progress_cb(
                "tool_start",
                "run_subagent",
                title,
                {"profile": profile, "task": task},
                subagent_id=self.agent_id,
            )
        except Exception:
            pass  # Fire-and-forget; degrade gracefully

    def event(self, ev: SubagentEvent) -> None:
        """Forward a subagent event as progress."""
        kind = ev.get("kind")

        # Build a preview text from the event
        preview = self._build_preview(ev)
        if not preview:
            return

        # For background, throttle updates
        if self.background:
            now = time.monotonic()
            if now - self._last_update_time < self._throttle_seconds:
                return
            self._last_update_time = now

        try:
            self.progress_cb(
                "progress",
                "run_subagent",
                preview,
                {},
                subagent_id=self.agent_id,
            )
        except Exception:
            pass

    def _build_preview(self, ev: SubagentEvent) -> str:
        """Build a short preview text from an event."""
        kind = ev.get("kind")

        if kind == "message":
            text = ev.get("text", "")
            return f"Message: {text[:80]}" if text else ""
        elif kind == "thought":
            text = ev.get("text", "")
            return f"Thought: {text[:80]}" if text else ""
        elif kind == "tool_call":
            title = ev.get("title", "tool")
            return f"Tool: {title}"
        elif kind == "tool_update":
            status = ev.get("status", "?")
            return f"Tool: {status}"
        elif kind == "diff":
            diff = ev.get("diff", {})
            path = diff.get("path", "file")
            return f"Edit: {path}"
        elif kind == "plan":
            return "Plan updated"
        elif kind == "usage":
            usage = ev.get("usage", {})
            tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            return f"Usage: {tokens} tokens"
        elif kind == "status":
            return ev.get("text", "Status update")

        return ""

    def request_permission(self, permission: dict[str, Any]) -> str:
        """Return OUTCOME_DENY (non-interactive)."""
        return OUTCOME_DENY

    def done(self, result: SubagentResult) -> None:
        """Emit a tool_complete or tool_error event."""
        try:
            event_type = "tool_complete" if result.status == "completed" else "tool_error"
            self.progress_cb(
                event_type,
                "run_subagent",
                result.summary[:200] if result.summary else "",
                {},
                subagent_id=self.agent_id,
            )
        except Exception:
            pass


def try_make_progress_sink(
    agent_id: str, *, background: bool = False, progress_cb: Callable | None = None
) -> SubagentSink | None:
    """Return a progress-callback sink iff a callback is available, else ``None``.

    Args:
        agent_id: The subagent's id
        background: If True, throttle updates
        progress_cb: Hermes tool_progress_callback or None

    Returns:
        A ProgressSubagentSink if progress_cb is not None, else None.
    """
    if progress_cb is None:
        return None
    return ProgressSubagentSink(agent_id, progress_cb, background=background)
