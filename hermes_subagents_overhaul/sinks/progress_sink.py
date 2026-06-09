"""CLI/TUI/gateway event sink — bridges subagent activity to a Hermes
``tool_progress_callback`` (the same callback ``delegate_task`` uses).

STUB / CONTRACT ONLY. The full implementation is the *sinks* workstream and OWNS
this file end-to-end (alongside ``acp_sink.py``). It must implement
:class:`~hermes_subagents_overhaul.sinks.base.SubagentSink`, forwarding start/event/
done as ``tool_progress_callback(event_type, tool_name, preview, args, **identity)``
calls (throttled for background) and returning a non-interactive
:data:`~hermes_subagents_overhaul.sinks.base.OUTCOME_DENY` from ``request_permission``
when no approver is wired.
"""

from __future__ import annotations

from typing import Callable

from hermes_subagents_overhaul.sinks.base import SubagentSink


def try_make_progress_sink(
    agent_id: str, *, background: bool = False, progress_cb: Callable | None = None
) -> SubagentSink | None:
    """Return a progress-callback sink iff a callback is available, else ``None``.

    STUB: returns ``None`` until the sinks workstream implements
    ``ProgressSubagentSink``.
    """
    if progress_cb is None:
        return None
    # TODO(sinks WS): build and return a ProgressSubagentSink(agent_id, progress_cb).
    return None
