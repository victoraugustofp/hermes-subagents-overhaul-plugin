"""Event sink abstraction — where subagent activity is surfaced.

A :class:`SubagentSink` is created **per subagent** (bound to that subagent's
stable umbrella ``toolCallId`` = its ``agent_id``) and receives the lifecycle:

    sink.start(...)          # once, at spawn
    sink.event(ev)           # for each normalized SubagentEvent
    sink.request_permission(perm) -> outcome   # foreground approvals only
    sink.done(result)        # once, at completion

Two concrete sinks live in sibling modules (owned by the *sinks* workstream):
  * ``acp_sink.ACPSubagentSink``      — emits ACP ``session/update`` via the
    optional ``hermes_acp_plugin.runtime`` bridge (rich for foreground, collapsed
    for background).
  * ``progress_sink.ProgressSubagentSink`` — bridges to a Hermes
    ``tool_progress_callback`` for CLI/TUI/gateway.

:func:`make_sink` picks the right one: ACP when an ACP session is active, else the
progress callback, else a :class:`NullSink` (always safe, never raises).
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from hermes_subagents_overhaul.backends.base import SubagentEvent, SubagentResult

# Permission outcomes the sink may return from request_permission().
OUTCOME_ALLOW = "allow"
OUTCOME_DENY = "deny"


@runtime_checkable
class SubagentSink(Protocol):
    """Receives one subagent's activity. Implementations MUST never raise."""

    def start(
        self,
        *,
        title: str,
        profile: str,
        backend: str,
        task: str,
        model: str | None = None,
    ) -> None: ...

    def event(self, ev: SubagentEvent) -> None: ...

    def request_permission(self, permission: dict[str, Any]) -> str:
        """Forward an approval prompt and return the chosen outcome.

        Returns an outcome string: :data:`OUTCOME_ALLOW`, :data:`OUTCOME_DENY`, or
        a specific ACP option id. Non-interactive sinks return :data:`OUTCOME_DENY`.
        """
        ...

    def done(self, result: SubagentResult) -> None: ...


class NullSink:
    """A sink that does nothing (used when no surface is available)."""

    def __init__(self, agent_id: str = "", *, background: bool = False) -> None:
        self.agent_id = agent_id
        self.background = background

    def start(self, **_: Any) -> None:  # noqa: D401
        return None

    def event(self, ev: SubagentEvent) -> None:
        return None

    def request_permission(self, permission: dict[str, Any]) -> str:
        return OUTCOME_DENY

    def done(self, result: SubagentResult) -> None:
        return None


def _try_acp_sink(agent_id: str, *, background: bool, throttle_seconds: float) -> SubagentSink | None:
    """Return an ACP sink iff the bridge is importable AND an ACP session is active."""
    try:
        from hermes_subagents_overhaul.sinks.acp_sink import try_make_acp_sink

        return try_make_acp_sink(agent_id, background=background, throttle_seconds=throttle_seconds)
    except Exception:
        return None


def _try_progress_sink(agent_id: str, *, background: bool, progress_cb: Callable | None) -> SubagentSink | None:
    try:
        from hermes_subagents_overhaul.sinks.progress_sink import try_make_progress_sink

        return try_make_progress_sink(agent_id, background=background, progress_cb=progress_cb)
    except Exception:
        return None


def make_sink(
    agent_id: str,
    *,
    background: bool = False,
    progress_cb: Callable | None = None,
    throttle_seconds: float = 1.0,
) -> SubagentSink:
    """Choose the best available sink for ``agent_id``.

    Order: ACP (if a session is active) -> progress callback -> :class:`NullSink`.
    Never raises; always returns a usable sink.
    """
    sink = _try_acp_sink(agent_id, background=background, throttle_seconds=throttle_seconds)
    if sink is not None:
        return sink
    sink = _try_progress_sink(agent_id, background=background, progress_cb=progress_cb)
    if sink is not None:
        return sink
    return NullSink(agent_id, background=background)
