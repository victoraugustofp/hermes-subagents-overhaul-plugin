"""ACP event sink — emits subagent activity as ACP ``session/update`` notifications
via the optional ``hermes_acp_plugin.runtime`` bridge (Project B).

STUB / CONTRACT ONLY. The full implementation is the *sinks* workstream and OWNS
this file end-to-end (alongside ``progress_sink.py``). It must implement
:class:`~hermes_subagents_overhaul.sinks.base.SubagentSink` with the PLAN.md §7
event mapping:

* Each subagent owns a stable umbrella ``toolCallId`` = its ``agent_id``.
* Background (collapsed): ``start`` -> ``start_tool_call(status="in_progress")``;
  ``event`` -> throttled ``update_tool_call(status="in_progress", content=[latest])``;
  ``done`` -> ``update_tool_call(status="completed"/"failed", content=[summary])``.
* Foreground (rich): also forward child ``tool_call``/``tool_update``/``diff`` as
  namespaced ``toolCallId = f"{agent_id}:{child_id}"``; relay ``request_permission``
  to the parent session and return the chosen outcome.

The bridge is SOFT-imported; if absent (or no active ACP session),
:func:`try_make_acp_sink` returns ``None`` so :func:`make_sink` falls back.
"""

from __future__ import annotations

from hermes_subagents_overhaul.sinks.base import SubagentSink


def try_make_acp_sink(
    agent_id: str, *, background: bool = False, throttle_seconds: float = 1.0
) -> SubagentSink | None:
    """Return an ACP sink iff an ACP session is active, else ``None``.

    STUB: returns ``None`` until the sinks workstream wires the
    ``hermes_acp_plugin.runtime.current_acp_session()`` bridge.
    """
    try:
        from hermes_acp_plugin.runtime import current_acp_session  # type: ignore
    except Exception:
        return None
    if current_acp_session() is None:
        return None
    # TODO(sinks WS): build and return an ACPSubagentSink bound to agent_id.
    return None
