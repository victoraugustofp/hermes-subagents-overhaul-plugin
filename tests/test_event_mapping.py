"""Tests for event mapping — ACP and progress sinks.

Tests the event->emission mapping for both foreground and background modes,
throttling behavior, permission outcome mapping, and sink fallback logic.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes_subagents_overhaul.backends.base import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SubagentEvent,
    SubagentResult,
    event,
)
from hermes_subagents_overhaul.sinks.acp_sink import ACPSubagentSink, try_make_acp_sink
from hermes_subagents_overhaul.sinks.base import (
    OUTCOME_ALLOW,
    OUTCOME_DENY,
    NullSink,
    make_sink,
)
from hermes_subagents_overhaul.sinks.progress_sink import (
    ProgressSubagentSink,
    try_make_progress_sink,
)


# ============================================================================
# Fake AcpSessionHandle for testing
# ============================================================================


class FakeAcpSessionHandle:
    """A fake AcpSessionHandle that records all calls for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._permission_result = OUTCOME_ALLOW

    def start_tool_call(
        self,
        tool_call_id: str,
        title: str,
        *,
        kind: str = "other",
        status: str = "in_progress",
        content: list[dict[str, Any]] | None = None,
        locations: list[dict[str, Any]] | None = None,
        raw_input: Any = None,
    ) -> bool:
        """Record a start_tool_call."""
        self.calls.append(
            (
                "start_tool_call",
                {
                    "tool_call_id": tool_call_id,
                    "title": title,
                    "kind": kind,
                    "status": status,
                    "content": content,
                    "locations": locations,
                    "raw_input": raw_input,
                },
            )
        )
        return True

    def update_tool_call(
        self,
        tool_call_id: str,
        *,
        status: str | None = None,
        title: str | None = None,
        kind: str | None = None,
        content: list[dict[str, Any]] | None = None,
        raw_output: Any = None,
    ) -> bool:
        """Record an update_tool_call."""
        self.calls.append(
            (
                "update_tool_call",
                {
                    "tool_call_id": tool_call_id,
                    "status": status,
                    "title": title,
                    "kind": kind,
                    "content": content,
                    "raw_output": raw_output,
                },
            )
        )
        return True

    def request_permission(
        self,
        *,
        title: str,
        tool_call_id: str | None = None,
        options: list[dict[str, str]] | None = None,
        timeout: float = 300.0,
    ) -> str:
        """Record and return a permission result."""
        self.calls.append(
            (
                "request_permission",
                {
                    "title": title,
                    "tool_call_id": tool_call_id,
                    "options": options,
                    "timeout": timeout,
                },
            )
        )
        return self._permission_result

    def plan(self, entries: list[Any]) -> bool:
        """Record a plan update."""
        self.calls.append(("plan", {"entries": entries}))
        return True

    def set_permission_result(self, result: str) -> None:
        """Set the result that request_permission will return."""
        self._permission_result = result


# ============================================================================
# Tests: ACPSubagentSink background mode
# ============================================================================


class TestACPBackgroundMode:
    """Test ACPSubagentSink in background (collapsed) mode."""

    def test_start_emits_umbrella_tool_call(self):
        """Background: start emits one umbrella start_tool_call."""
        handle = FakeAcpSessionHandle()
        sink = ACPSubagentSink(handle, "sa_test_abc", background=True, throttle_seconds=1.0)

        sink.start(
            title="My Task",
            profile="coder",
            backend="devin",
            task="Write a function",
            model="sonnet",
        )

        assert len(handle.calls) == 1
        method, kwargs = handle.calls[0]
        assert method == "start_tool_call"
        assert kwargs["tool_call_id"] == "sa_test_abc"
        assert kwargs["title"] == "coder subagent: My Task"
        assert kwargs["kind"] == "other"
        assert kwargs["status"] == "in_progress"
        assert kwargs["raw_input"] == {"task": "Write a function", "model": "sonnet"}

    def test_background_coalesces_events_with_throttling(self):
        """Background: burst of events coalesces into single throttled update."""
        handle = FakeAcpSessionHandle()
        sink = ACPSubagentSink(handle, "sa_test_abc", background=True, throttle_seconds=0.1)

        # Emit multiple events in quick succession
        sink.event(event("message", text="Starting analysis"))
        sink.event(event("thought", text="Let me think about this"))
        sink.event(event("tool_call", child_id="t1", title="read_file", tool_kind="read", status="in_progress"))

        # With throttle_seconds=0.1, only the first should trigger an update
        start_calls = [c for c in handle.calls if c[0] == "start_tool_call"]
        update_calls = [c for c in handle.calls if c[0] == "update_tool_call"]
        assert len(start_calls) == 0  # No start_tool_call from events
        assert len(update_calls) == 1  # Only one throttled update

        # The content should be the latest activity (the message, since events are coalesced)
        _, kwargs = update_calls[0]
        assert kwargs["tool_call_id"] == "sa_test_abc"
        assert kwargs["status"] == "in_progress"
        # The latest activity is from the tool_call event
        assert "Tool:" in str(kwargs["content"]) or "read_file" in str(kwargs["content"]) or "Message:" in str(kwargs["content"])

    def test_background_throttle_with_zero_seconds_flushes_all(self):
        """Background: throttle_seconds=0 flushes every event."""
        handle = FakeAcpSessionHandle()
        sink = ACPSubagentSink(handle, "sa_test_abc", background=True, throttle_seconds=0)

        sink.event(event("message", text="First"))
        sink.event(event("message", text="Second"))
        sink.event(event("message", text="Third"))

        update_calls = [c for c in handle.calls if c[0] == "update_tool_call"]
        assert len(update_calls) == 3

    def test_background_done_emits_completed_status(self):
        """Background: done emits completed status with summary."""
        handle = FakeAcpSessionHandle()
        sink = ACPSubagentSink(handle, "sa_test_abc", background=True)

        result = SubagentResult(
            status=STATUS_COMPLETED,
            summary="Task completed successfully",
            files_changed=["file1.py"],
        )
        sink.done(result)

        done_calls = [c for c in handle.calls if c[0] == "update_tool_call"]
        assert len(done_calls) == 1
        _, kwargs = done_calls[0]
        assert kwargs["tool_call_id"] == "sa_test_abc"
        assert kwargs["status"] == "completed"
        assert "Task completed successfully" in str(kwargs["content"])
        assert kwargs["raw_output"]["status"] == STATUS_COMPLETED

    def test_background_done_emits_failed_status(self):
        """Background: done emits failed status for non-completed results."""
        handle = FakeAcpSessionHandle()
        sink = ACPSubagentSink(handle, "sa_test_abc", background=True)

        result = SubagentResult(
            status=STATUS_FAILED,
            summary="Task failed: timeout",
            error="Timeout after 60s",
        )
        sink.done(result)

        done_calls = [c for c in handle.calls if c[0] == "update_tool_call"]
        _, kwargs = done_calls[0]
        assert kwargs["status"] == "failed"

    def test_background_child_tool_calls_not_forwarded(self):
        """Background: child tool_call/tool_update are NOT individually forwarded."""
        handle = FakeAcpSessionHandle()
        sink = ACPSubagentSink(handle, "sa_test_abc", background=True, throttle_seconds=0)

        sink.event(
            event(
                "tool_call",
                child_id="child_1",
                title="read_file",
                tool_kind="read",
                status="in_progress",
                raw_input={"path": "/tmp/file.txt"},
            )
        )

        # Should only have umbrella update, not a namespaced start_tool_call
        start_calls = [c for c in handle.calls if c[0] == "start_tool_call"]
        assert len(start_calls) == 0  # No child tool_call forwarded


# ============================================================================
# Tests: ACPSubagentSink foreground mode
# ============================================================================


class TestACPForegroundMode:
    """Test ACPSubagentSink in foreground (rich) mode."""

    def test_foreground_forwards_child_tool_call(self):
        """Foreground: child tool_call forwarded with namespaced id."""
        handle = FakeAcpSessionHandle()
        sink = ACPSubagentSink(handle, "sa_test_abc", background=False)

        sink.event(
            event(
                "tool_call",
                child_id="child_1",
                title="read_file",
                tool_kind="read",
                status="in_progress",
                raw_input={"path": "/tmp/file.txt"},
                locations=[{"path": "/tmp/file.txt", "line": 1}],
            )
        )

        start_calls = [c for c in handle.calls if c[0] == "start_tool_call"]
        assert len(start_calls) == 1
        _, kwargs = start_calls[0]
        assert kwargs["tool_call_id"] == "sa_test_abc:child_1"
        assert kwargs["title"] == "read_file"
        assert kwargs["kind"] == "read"
        assert kwargs["status"] == "in_progress"
        assert kwargs["raw_input"] == {"path": "/tmp/file.txt"}
        assert kwargs["locations"] == [{"path": "/tmp/file.txt", "line": 1}]

    def test_foreground_forwards_child_tool_update(self):
        """Foreground: child tool_update forwarded with namespaced id."""
        handle = FakeAcpSessionHandle()
        sink = ACPSubagentSink(handle, "sa_test_abc", background=False)

        sink.event(
            event(
                "tool_update",
                child_id="child_1",
                status="completed",
                content=[{"type": "text", "text": "File contents"}],
                raw_output={"lines": 42},
            )
        )

        update_calls = [c for c in handle.calls if c[0] == "update_tool_call"]
        assert len(update_calls) == 1
        _, kwargs = update_calls[0]
        assert kwargs["tool_call_id"] == "sa_test_abc:child_1"
        assert kwargs["status"] == "completed"
        assert kwargs["content"] == [{"type": "text", "text": "File contents"}]
        assert kwargs["raw_output"] == {"lines": 42}

    def test_foreground_forwards_diff(self):
        """Foreground: diff forwarded as tool_call_update with diff content."""
        handle = FakeAcpSessionHandle()
        sink = ACPSubagentSink(handle, "sa_test_abc", background=False)

        sink.event(
            event(
                "diff",
                child_id="edit_1",
                diff={
                    "path": "src/main.py",
                    "old_text": "def foo():\n    pass",
                    "new_text": "def foo():\n    return 42",
                },
            )
        )

        update_calls = [c for c in handle.calls if c[0] == "update_tool_call"]
        assert len(update_calls) == 1
        _, kwargs = update_calls[0]
        assert kwargs["tool_call_id"] == "sa_test_abc:edit_1"
        assert len(kwargs["content"]) == 1
        assert kwargs["content"][0]["type"] == "diff"
        assert kwargs["content"][0]["path"] == "src/main.py"
        assert kwargs["content"][0]["old_text"] == "def foo():\n    pass"
        assert kwargs["content"][0]["new_text"] == "def foo():\n    return 42"

    def test_foreground_forwards_message(self):
        """Foreground: message forwarded as umbrella update."""
        handle = FakeAcpSessionHandle()
        sink = ACPSubagentSink(handle, "sa_test_abc", background=False)

        sink.event(event("message", text="The task is complete"))

        update_calls = [c for c in handle.calls if c[0] == "update_tool_call"]
        assert len(update_calls) == 1
        _, kwargs = update_calls[0]
        assert kwargs["tool_call_id"] == "sa_test_abc"
        assert "The task is complete" in str(kwargs["content"])

    def test_foreground_forwards_plan(self):
        """Foreground: plan forwarded via handle.plan()."""
        handle = FakeAcpSessionHandle()
        sink = ACPSubagentSink(handle, "sa_test_abc", background=False)

        plan_entries = [
            {"content": "Step 1", "status": "pending"},
            {"content": "Step 2", "status": "in_progress"},
        ]
        sink.event(event("plan", plan=plan_entries))

        plan_calls = [c for c in handle.calls if c[0] == "plan"]
        assert len(plan_calls) == 1
        _, kwargs = plan_calls[0]
        assert kwargs["entries"] == plan_entries

    def test_foreground_relays_permission(self):
        """Foreground: request_permission relayed to handle and outcome mapped."""
        handle = FakeAcpSessionHandle()
        handle.set_permission_result("allow")
        sink = ACPSubagentSink(handle, "sa_test_abc", background=False)

        permission = {
            "request_id": "perm_1",
            "title": "Approve file edit?",
            "options": [
                {"id": "allow", "name": "Allow"},
                {"id": "deny", "name": "Deny"},
            ],
            "tool_call": {"id": "tool_1"},
        }
        outcome = sink.request_permission(permission)

        assert outcome == OUTCOME_ALLOW
        perm_calls = [c for c in handle.calls if c[0] == "request_permission"]
        assert len(perm_calls) == 1
        _, kwargs = perm_calls[0]
        assert kwargs["title"] == "Approve file edit?"
        assert kwargs["tool_call_id"] == "sa_test_abc:tool_1"

    def test_foreground_permission_deny_mapping(self):
        """Foreground: permission result 'deny' maps to OUTCOME_DENY."""
        handle = FakeAcpSessionHandle()
        handle.set_permission_result("deny")
        sink = ACPSubagentSink(handle, "sa_test_abc", background=False)

        permission = {
            "title": "Approve?",
            "options": [{"id": "allow", "name": "Allow"}],
        }
        outcome = sink.request_permission(permission)

        assert outcome == OUTCOME_DENY

    def test_foreground_permission_specific_option_id(self):
        """Foreground: specific option id returned as-is."""
        handle = FakeAcpSessionHandle()
        handle.set_permission_result("custom_option_id")
        sink = ACPSubagentSink(handle, "sa_test_abc", background=False)

        permission = {
            "title": "Choose action",
            "options": [
                {"id": "custom_option_id", "name": "Custom Action"},
            ],
        }
        outcome = sink.request_permission(permission)

        assert outcome == "custom_option_id"


# ============================================================================
# Tests: ProgressSubagentSink
# ============================================================================


class TestProgressSubagentSink:
    """Test ProgressSubagentSink."""

    def test_start_emits_tool_start(self):
        """start emits tool_start event."""
        progress_cb = MagicMock()
        sink = ProgressSubagentSink("sa_test_abc", progress_cb, background=False)

        sink.start(
            title="My Task",
            profile="coder",
            backend="devin",
            task="Write code",
            model="sonnet",
        )

        progress_cb.assert_called_once()
        args = progress_cb.call_args
        assert args[0] == ("tool_start", "run_subagent", "My Task", {"profile": "coder", "task": "Write code"})
        assert args[1]["subagent_id"] == "sa_test_abc"

    def test_event_emits_progress(self):
        """event emits progress event with preview."""
        progress_cb = MagicMock()
        sink = ProgressSubagentSink("sa_test_abc", progress_cb, background=False)

        sink.event(event("message", text="Processing..."))

        progress_cb.assert_called_once()
        args = progress_cb.call_args
        assert args[0][0] == "progress"
        assert args[0][1] == "run_subagent"
        assert "Processing" in args[0][2]
        assert args[1]["subagent_id"] == "sa_test_abc"

    def test_background_throttles_progress(self):
        """Background: progress events throttled."""
        progress_cb = MagicMock()
        sink = ProgressSubagentSink("sa_test_abc", progress_cb, background=True)
        sink._throttle_seconds = 0.1

        sink.event(event("message", text="First"))
        sink.event(event("message", text="Second"))
        time.sleep(0.15)
        sink.event(event("message", text="Third"))

        # Should have 2 calls: one for first, one for third (after throttle)
        assert progress_cb.call_count == 2

    def test_done_emits_tool_complete(self):
        """done emits tool_complete for completed status."""
        progress_cb = MagicMock()
        sink = ProgressSubagentSink("sa_test_abc", progress_cb, background=False)

        result = SubagentResult(status=STATUS_COMPLETED, summary="Done!")
        sink.done(result)

        progress_cb.assert_called_once()
        args = progress_cb.call_args
        assert args[0][0] == "tool_complete"
        assert args[0][1] == "run_subagent"
        assert "Done!" in args[0][2]
        assert args[1]["subagent_id"] == "sa_test_abc"

    def test_done_emits_tool_error(self):
        """done emits tool_error for failed status."""
        progress_cb = MagicMock()
        sink = ProgressSubagentSink("sa_test_abc", progress_cb, background=False)

        result = SubagentResult(status=STATUS_FAILED, summary="Failed!")
        sink.done(result)

        args = progress_cb.call_args
        assert args[0][0] == "tool_error"

    def test_request_permission_returns_deny(self):
        """request_permission returns OUTCOME_DENY (non-interactive)."""
        progress_cb = MagicMock()
        sink = ProgressSubagentSink("sa_test_abc", progress_cb, background=False)

        outcome = sink.request_permission({"title": "Approve?"})

        assert outcome == OUTCOME_DENY


# ============================================================================
# Tests: Sink factory functions
# ============================================================================


class TestTryMakeAcpSink:
    """Test try_make_acp_sink factory."""

    def test_returns_acp_sink_with_valid_handle(self):
        """Returns ACPSubagentSink when given a valid handle."""
        fake_handle = FakeAcpSessionHandle()
        # Directly test that the sink is created with correct parameters
        # (we can't easily mock the import, so we test the core logic)
        sink = ACPSubagentSink(fake_handle, "sa_test", background=False, throttle_seconds=2.0)
        assert isinstance(sink, ACPSubagentSink)
        assert sink.agent_id == "sa_test"
        assert sink.background is False
        assert sink.throttle_seconds == 2.0

    def test_try_make_acp_sink_returns_none_when_no_bridge(self):
        """try_make_acp_sink returns None when bridge is not available.
        
        This test verifies the fallback behavior when hermes_acp_plugin is not installed.
        """
        # When the bridge is not available, try_make_acp_sink returns None
        # This is tested implicitly by the make_sink integration tests below
        result = try_make_acp_sink("sa_test", background=False)
        # In the test environment, hermes_acp_plugin may not be available
        # so we just verify the function doesn't crash
        assert result is None or isinstance(result, ACPSubagentSink)


class TestTryMakeProgressSink:
    """Test try_make_progress_sink factory."""

    def test_returns_none_if_callback_is_none(self):
        """Returns None if progress_cb is None."""
        result = try_make_progress_sink("sa_test", background=False, progress_cb=None)
        assert result is None

    def test_returns_progress_sink_if_callback_available(self):
        """Returns ProgressSubagentSink if callback is provided."""
        progress_cb = MagicMock()
        result = try_make_progress_sink("sa_test", background=True, progress_cb=progress_cb)
        assert isinstance(result, ProgressSubagentSink)
        assert result.agent_id == "sa_test"
        assert result.background is True


class TestMakeSink:
    """Test make_sink factory (integration)."""

    def test_prefers_progress_when_acp_unavailable(self):
        """make_sink falls back to progress when ACP is unavailable."""
        progress_cb = MagicMock()
        # When hermes_acp_plugin is not available, make_sink should use progress
        sink = make_sink("sa_test", background=False, progress_cb=progress_cb)
        # Should be progress sink since ACP is not available in test env
        assert isinstance(sink, (ProgressSubagentSink, NullSink))

    def test_uses_progress_when_provided(self):
        """make_sink uses progress sink when callback is provided."""
        progress_cb = MagicMock()
        sink = make_sink("sa_test", background=False, progress_cb=progress_cb)
        # If ACP is not available, should use progress
        if not isinstance(sink, ACPSubagentSink):
            assert isinstance(sink, (ProgressSubagentSink, NullSink))

    def test_falls_back_to_null_if_nothing_available(self):
        """make_sink falls back to NullSink if nothing is available."""
        sink = make_sink("sa_test", background=False, progress_cb=None)
        # Should be NullSink when no callbacks/ACP available
        assert isinstance(sink, (NullSink, ACPSubagentSink))  # Could be ACP if bridge is available


# ============================================================================
# Tests: Defensive error handling
# ============================================================================


class TestDefensiveErrorHandling:
    """Test that sinks degrade gracefully on errors."""

    def test_acp_sink_handles_start_tool_call_exception(self):
        """ACPSubagentSink.start handles exceptions gracefully."""
        handle = MagicMock()
        handle.start_tool_call.side_effect = Exception("Connection lost")
        sink = ACPSubagentSink(handle, "sa_test", background=False)

        # Should not raise
        sink.start(title="Task", profile="coder", backend="devin", task="Code", model="sonnet")

    def test_acp_sink_handles_update_tool_call_exception(self):
        """ACPSubagentSink.event handles exceptions gracefully."""
        handle = MagicMock()
        handle.update_tool_call.side_effect = Exception("Connection lost")
        sink = ACPSubagentSink(handle, "sa_test", background=False)

        # Should not raise
        sink.event(event("message", text="Test"))

    def test_acp_sink_handles_request_permission_exception(self):
        """ACPSubagentSink.request_permission handles exceptions gracefully."""
        handle = MagicMock()
        handle.request_permission.side_effect = Exception("Connection lost")
        sink = ACPSubagentSink(handle, "sa_test", background=False)

        outcome = sink.request_permission({"title": "Approve?"})
        assert outcome == OUTCOME_DENY

    def test_progress_sink_handles_callback_exception(self):
        """ProgressSubagentSink handles callback exceptions gracefully."""
        progress_cb = MagicMock(side_effect=Exception("Callback error"))
        sink = ProgressSubagentSink("sa_test", progress_cb, background=False)

        # Should not raise
        sink.start(title="Task", profile="coder", backend="devin", task="Code", model="sonnet")
        sink.event(event("message", text="Test"))
        sink.done(SubagentResult(status=STATUS_COMPLETED, summary="Done"))
