"""ACP event sink — emits subagent activity as ACP ``session/update`` notifications
via the optional ``hermes_acp_plugin.runtime`` bridge (Project B).

Implements :class:`ACPSubagentSink` per PLAN.md §7 event mapping:

* Each subagent owns a stable umbrella ``toolCallId`` = its ``agent_id``.
* Background (collapsed): ``start`` -> ``start_tool_call(status="in_progress")``;
  ``event`` -> throttled ``update_tool_call(status="in_progress", content=[latest])``;
  ``done`` -> ``update_tool_call(status="completed"/"failed", content=[summary])``.
* Foreground (rich): also forward child ``tool_call``/``tool_update``/``diff`` as
  namespaced ``toolCallId = f"{agent_id}:{child_id}"``; relay ``request_permission``
  to the parent session and return the chosen outcome.
"""

from __future__ import annotations

import time
from typing import Any

from hermes_subagents_overhaul.backends.base import SubagentEvent, SubagentResult
from hermes_subagents_overhaul.sinks.base import OUTCOME_ALLOW, OUTCOME_DENY, SubagentSink


class ACPSubagentSink:
    """Emits subagent activity to ACP via the hermes_acp_plugin.runtime bridge."""

    def __init__(
        self,
        handle: Any,  # AcpSessionHandle from hermes_acp_plugin.runtime
        agent_id: str,
        background: bool = False,
        throttle_seconds: float = 1.0,
    ) -> None:
        """Initialize the ACP sink.

        Args:
            handle: AcpSessionHandle from current_acp_session()
            agent_id: The subagent's stable umbrella toolCallId
            background: If True, collapse activity; if False, forward granularly
            throttle_seconds: Minimum seconds between throttled updates
        """
        self.handle = handle
        self.agent_id = agent_id
        self.background = background
        self.throttle_seconds = throttle_seconds

        # For background mode: track latest activity and throttle updates
        self._last_update_time = 0.0
        self._latest_activity = ""

    def start(
        self,
        *,
        title: str,
        profile: str,
        backend: str,
        task: str,
        model: str | None = None,
    ) -> None:
        """Emit the umbrella tool_call on subagent spawn."""
        try:
            self.handle.start_tool_call(
                self.agent_id,
                title=f"{profile} subagent: {title}",
                kind="other",
                status="in_progress",
                raw_input={"task": task, "model": model},
            )
        except Exception:
            pass  # Fire-and-forget; degrade gracefully

    def event(self, ev: SubagentEvent) -> None:
        """Forward a subagent event to ACP."""
        if self.background:
            self._handle_background_event(ev)
        else:
            self._handle_foreground_event(ev)

    def _handle_background_event(self, ev: SubagentEvent) -> None:
        """Background mode: coalesce activity into umbrella updates with throttling."""
        kind = ev.get("kind")

        # Update the latest activity line
        if kind == "message":
            self._latest_activity = f"Message: {ev.get('text', '')[:100]}"
        elif kind == "thought":
            self._latest_activity = f"Thought: {ev.get('text', '')[:100]}"
        elif kind == "tool_call":
            child_id = ev.get("child_id", "?")
            title = ev.get("title", "tool")
            self._latest_activity = f"Tool: {title} ({child_id})"
        elif kind == "tool_update":
            child_id = ev.get("child_id", "?")
            status = ev.get("status", "?")
            self._latest_activity = f"Tool {child_id}: {status}"
        elif kind == "diff":
            diff = ev.get("diff", {})
            path = diff.get("path", "file")
            self._latest_activity = f"Edit: {path}"
        elif kind == "plan":
            self._latest_activity = "Plan updated"
        elif kind == "usage":
            usage = ev.get("usage", {})
            tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            self._latest_activity = f"Usage: {tokens} tokens"
        elif kind == "status":
            self._latest_activity = ev.get("text", "Status update")

        # Throttle updates: send at most once per throttle_seconds
        now = time.monotonic()
        if now - self._last_update_time >= self.throttle_seconds:
            self._flush_background_update()
            self._last_update_time = now

    def _flush_background_update(self) -> None:
        """Emit a throttled background update with the latest activity."""
        if not self._latest_activity:
            return
        try:
            self.handle.update_tool_call(
                self.agent_id,
                status="in_progress",
                content=[{"type": "text", "text": self._latest_activity}],
            )
        except Exception:
            pass

    def _handle_foreground_event(self, ev: SubagentEvent) -> None:
        """Foreground mode: forward granular child activity with namespaced ids."""
        kind = ev.get("kind")

        if kind == "tool_call":
            # Forward child tool_call with namespaced id
            child_id = ev.get("child_id", "")
            tool_call_id = f"{self.agent_id}:{child_id}"
            try:
                self.handle.start_tool_call(
                    tool_call_id,
                    title=ev.get("title", "tool"),
                    kind=ev.get("tool_kind", "other"),
                    status=ev.get("status", "in_progress"),
                    raw_input=ev.get("raw_input"),
                    locations=ev.get("locations"),
                )
            except Exception:
                pass

        elif kind == "tool_update":
            # Forward child tool_update with namespaced id
            child_id = ev.get("child_id", "")
            tool_call_id = f"{self.agent_id}:{child_id}"
            try:
                self.handle.update_tool_call(
                    tool_call_id,
                    status=ev.get("status"),
                    content=ev.get("content"),
                    raw_output=ev.get("raw_output"),
                )
            except Exception:
                pass

        elif kind == "diff":
            # Forward diff as tool_call_update content
            diff = ev.get("diff", {})
            child_id = ev.get("child_id", "edit")
            tool_call_id = f"{self.agent_id}:{child_id}"
            try:
                self.handle.update_tool_call(
                    tool_call_id,
                    content=[
                        {
                            "type": "diff",
                            "path": diff.get("path", ""),
                            "old_text": diff.get("old_text", ""),
                            "new_text": diff.get("new_text", ""),
                        }
                    ],
                )
            except Exception:
                pass

        elif kind == "message":
            # Forward message as umbrella update
            text = ev.get("text", "")
            try:
                self.handle.update_tool_call(
                    self.agent_id,
                    status="in_progress",
                    content=[{"type": "text", "text": text}],
                )
            except Exception:
                pass

        elif kind == "thought":
            # Forward thought as umbrella update
            text = ev.get("text", "")
            try:
                self.handle.update_tool_call(
                    self.agent_id,
                    status="in_progress",
                    content=[{"type": "text", "text": text}],
                )
            except Exception:
                pass

        elif kind == "plan":
            # Forward plan entries
            plan = ev.get("plan", [])
            try:
                self.handle.plan(plan)
            except Exception:
                pass

    def request_permission(self, permission: dict[str, Any]) -> str:
        """Forward an approval prompt and return the chosen outcome.

        Maps the returned option id to OUTCOME_ALLOW/OUTCOME_DENY:
        - "allow"/"approve"/selected non-deny option -> OUTCOME_ALLOW
        - "deny"/"cancelled"/"reject" -> OUTCOME_DENY

        Returns the option id if a specific one was chosen, else the outcome.
        """
        try:
            title = permission.get("title", "Approval required")
            tool_call_id = permission.get("tool_call", {}).get("id")
            options = permission.get("options")

            # Call the handle's request_permission
            result = self.handle.request_permission(
                title=title,
                tool_call_id=f"{self.agent_id}:{tool_call_id}" if tool_call_id else None,
                options=options,
            )

            # Map the result to an outcome
            if isinstance(result, str):
                result_lower = result.lower()
                if result_lower in ("deny", "cancelled", "reject"):
                    return OUTCOME_DENY
                elif result_lower in ("allow", "approve"):
                    return OUTCOME_ALLOW
                else:
                    # Specific option id was chosen; return it
                    return result

            return OUTCOME_DENY
        except Exception:
            # Degrade gracefully
            return OUTCOME_DENY

    def done(self, result: SubagentResult) -> None:
        """Emit the final tool_call_update with completion status."""
        try:
            status = "completed" if result.status == "completed" else "failed"
            summary = result.summary[:4000] if result.summary else ""
            self.handle.update_tool_call(
                self.agent_id,
                status=status,
                content=[{"type": "text", "text": summary}],
                raw_output=result.to_dict(),
            )
        except Exception:
            pass


def try_make_acp_sink(
    agent_id: str, *, background: bool = False, throttle_seconds: float = 1.0
) -> SubagentSink | None:
    """Return an ACP sink iff an ACP session is active, else ``None``.

    Soft-imports the hermes_acp_plugin.runtime bridge and checks if a session
    is currently active. If so, returns an ACPSubagentSink; otherwise None.
    """
    try:
        from hermes_acp_plugin.runtime import current_acp_session  # type: ignore
    except Exception:
        return None

    handle = current_acp_session()
    if handle is None:
        return None

    return ACPSubagentSink(handle, agent_id, background=background, throttle_seconds=throttle_seconds)
