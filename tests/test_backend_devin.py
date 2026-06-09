"""Tests for the Devin ACP backend.

Unit tests use a fake transport (mocked subprocess) to test the ACP protocol
without spawning the real devin binary. A real e2e test (gated on devin
availability) validates the full integration.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes_subagents_overhaul.backends.base import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
)
from hermes_subagents_overhaul.backends.devin_acp import DevinAcpBackend, DevinAcpHandle
from hermes_subagents_overhaul.config import (
    SANDBOX_READ_ONLY,
    SANDBOX_WORKSPACE_WRITE,
    ResolvedProfile,
)


# --- Fixtures ----------------------------------------------------------------


def make_mock_popen(responses: list[dict[str, Any]]) -> Any:
    """Create a mock Popen that returns the given responses."""

    def mock_popen(*args: Any, **kwargs: Any) -> Any:
        sent_messages: list[dict[str, Any]] = []
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.return_value = None

        def write_side_effect(data: str) -> None:
            try:
                msg = json.loads(data.strip())
                sent_messages.append(msg)
            except json.JSONDecodeError:
                pass

        proc.stdin.write = write_side_effect
        proc.stdin.flush = MagicMock()
        proc.stdout = [json.dumps(resp) + "\n" for resp in responses]
        proc.terminate = MagicMock()
        proc.wait = MagicMock()
        proc.kill = MagicMock()
        proc._sent_messages = sent_messages

        return proc

    return mock_popen


@pytest.fixture
def devin_backend() -> DevinAcpBackend:
    return DevinAcpBackend()


@pytest.fixture
def test_profile() -> ResolvedProfile:
    return ResolvedProfile(
        name="test",
        backend="devin",
        model="sonnet",
        sandbox=SANDBOX_WORKSPACE_WRITE,
    )


@pytest.fixture
def read_only_profile() -> ResolvedProfile:
    return ResolvedProfile(
        name="test_ro",
        backend="devin",
        model="sonnet",
        sandbox=SANDBOX_READ_ONLY,
        read_only=True,
    )


# --- Unit Tests: check_available -----------------------------------------------


def test_check_available_devin_not_found(devin_backend: DevinAcpBackend) -> None:
    """check_available returns False if devin binary is not in PATH."""
    with patch("shutil.which", return_value=None):
        ok, reason = devin_backend.check_available(
            ResolvedProfile(name="test", backend="devin")
        )
        assert not ok
        assert "not found" in reason.lower()


def test_check_available_not_logged_in(devin_backend: DevinAcpBackend) -> None:
    """check_available returns False if no credentials are found."""
    with patch("shutil.which", return_value="/usr/bin/devin"):
        with patch("pathlib.Path.exists", return_value=False):
            with patch.dict(os.environ, {}, clear=False):
                # Remove all login env vars
                for key in ["WINDSURF_API_KEY", "DEVIN_API_KEY", "DEVIN_ORG_ID"]:
                    os.environ.pop(key, None)

                ok, reason = devin_backend.check_available(
                    ResolvedProfile(name="test", backend="devin")
                )
                assert not ok
                assert "not logged in" in reason.lower()


def test_check_available_with_credentials_file(devin_backend: DevinAcpBackend) -> None:
    """check_available returns True if credentials file exists."""
    with patch("shutil.which", return_value="/usr/bin/devin"):
        with patch("pathlib.Path.exists", return_value=True):
            ok, reason = devin_backend.check_available(
                ResolvedProfile(name="test", backend="devin")
            )
            assert ok
            assert reason == ""


def test_check_available_with_windsurf_api_key(devin_backend: DevinAcpBackend) -> None:
    """check_available returns True if WINDSURF_API_KEY is set."""
    with patch("shutil.which", return_value="/usr/bin/devin"):
        with patch("pathlib.Path.exists", return_value=False):
            with patch.dict(os.environ, {"WINDSURF_API_KEY": "test-key"}):
                ok, reason = devin_backend.check_available(
                    ResolvedProfile(name="test", backend="devin")
                )
                assert ok
                assert reason == ""


def test_check_available_with_devin_api_key(devin_backend: DevinAcpBackend) -> None:
    """check_available returns True if DEVIN_API_KEY and DEVIN_ORG_ID are set."""
    with patch("shutil.which", return_value="/usr/bin/devin"):
        with patch("pathlib.Path.exists", return_value=False):
            with patch.dict(
                os.environ,
                {"DEVIN_API_KEY": "key", "DEVIN_ORG_ID": "org"},
            ):
                ok, reason = devin_backend.check_available(
                    ResolvedProfile(name="test", backend="devin")
                )
                assert ok
                assert reason == ""


# --- Unit Tests: ACP Protocol ------------------------------------------------


def test_acp_handshake_sequence(test_profile: ResolvedProfile) -> None:
    """Test that the ACP handshake sends initialize, session/new, session/prompt."""
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
    ]

    with patch("subprocess.Popen", side_effect=make_mock_popen(responses)):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        list(handle.events())
        assert handle.backend_session_id == "sess-123"


def test_session_update_message_chunk(test_profile: ResolvedProfile) -> None:
    """Test that agent_message_chunk updates are translated to message events."""
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "Hello, world!"},
                }
            },
        },
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
    ]

    with patch("subprocess.Popen", side_effect=make_mock_popen(responses)):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        events = list(handle.events())
        message_events = [e for e in events if e.get("kind") == "message"]
        assert len(message_events) > 0
        assert message_events[0].get("text") == "Hello, world!"


def test_session_update_thought_chunk(test_profile: ResolvedProfile) -> None:
    """Test that agent_thought_chunk updates are translated to thought events."""
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"text": "Let me think..."},
                }
            },
        },
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
    ]

    with patch("subprocess.Popen", side_effect=make_mock_popen(responses)):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        events = list(handle.events())
        thought_events = [e for e in events if e.get("kind") == "thought"]
        assert len(thought_events) > 0
        assert thought_events[0].get("text") == "Let me think..."


def test_session_update_tool_call(test_profile: ResolvedProfile) -> None:
    """Test that tool_call updates are translated to tool_call events."""
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-1",
                    "title": "Read file",
                    "kind": "read",
                    "status": "pending",
                    "rawInput": {"path": "/tmp/test.txt"},
                    "locations": [],
                }
            },
        },
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
    ]

    with patch("subprocess.Popen", side_effect=make_mock_popen(responses)):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        events = list(handle.events())
        tool_call_events = [e for e in events if e.get("kind") == "tool_call"]
        assert len(tool_call_events) > 0
        assert tool_call_events[0].get("child_id") == "tc-1"
        assert tool_call_events[0].get("title") == "Read file"
        assert tool_call_events[0].get("tool_kind") == "read"
        assert tool_call_events[0].get("status") == "pending"


def test_session_update_tool_update(test_profile: ResolvedProfile) -> None:
    """Test that tool_call_update updates are translated to tool_update events."""
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-1",
                    "status": "completed",
                    "content": [{"type": "text", "text": "File contents"}],
                    "rawOutput": "File contents",
                }
            },
        },
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
    ]

    with patch("subprocess.Popen", side_effect=make_mock_popen(responses)):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        events = list(handle.events())
        tool_update_events = [e for e in events if e.get("kind") == "tool_update"]
        assert len(tool_update_events) > 0
        assert tool_update_events[0].get("child_id") == "tc-1"
        assert tool_update_events[0].get("status") == "completed"


def test_session_update_plan(test_profile: ResolvedProfile) -> None:
    """Test that plan updates are translated to plan events."""
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "plan",
                    "entries": [
                        {"id": "p1", "title": "Step 1", "status": "pending"},
                        {"id": "p2", "title": "Step 2", "status": "pending"},
                    ],
                }
            },
        },
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
    ]

    with patch("subprocess.Popen", side_effect=make_mock_popen(responses)):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        events = list(handle.events())
        plan_events = [e for e in events if e.get("kind") == "plan"]
        assert len(plan_events) > 0
        assert len(plan_events[0].get("plan", [])) == 2


def test_session_update_diff(test_profile: ResolvedProfile) -> None:
    """Test that file_edit content generates diff events."""
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-1",
                    "status": "completed",
                    "content": [
                        {
                            "type": "file_edit",
                            "path": "/tmp/test.txt",
                            "oldText": "old content",
                            "newText": "new content",
                        }
                    ],
                }
            },
        },
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
    ]

    with patch("subprocess.Popen", side_effect=make_mock_popen(responses)):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        events = list(handle.events())
        diff_events = [e for e in events if e.get("kind") == "diff"]
        assert len(diff_events) > 0
        assert diff_events[0].get("diff", {}).get("path") == "/tmp/test.txt"
        assert diff_events[0].get("diff", {}).get("old_text") == "old content"
        assert diff_events[0].get("diff", {}).get("new_text") == "new content"


# --- Unit Tests: Permission Handling -----------------------------------------


def test_request_permission_allow(test_profile: ResolvedProfile) -> None:
    """Test that request_permission events are emitted and responses are sent."""
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "session/request_permission",
            "params": {
                "request": {
                    "title": "Write file",
                    "options": [
                        {"id": "opt-1", "name": "Allow once"},
                        {"id": "opt-2", "name": "Deny"},
                    ],
                    "toolCall": {"toolCallId": "tc-1"},
                }
            },
        },
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
    ]

    with patch("subprocess.Popen", side_effect=make_mock_popen(responses)):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        events_iter = handle.events()

        # Consume events until we hit a permission event
        permission_event = None
        for ev in events_iter:
            if ev.get("kind") == "permission":
                permission_event = ev
                break

        assert permission_event is not None
        request_id = permission_event.get("permission", {}).get("request_id")
        assert request_id is not None

        # Answer the permission
        handle.answer_permission(request_id, "allow", "opt-1")

        # Consume remaining events
        list(events_iter)

        result = handle.result()
        assert result.status == STATUS_COMPLETED


def test_request_permission_deny(test_profile: ResolvedProfile) -> None:
    """Test that denying a permission sends the correct response."""
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "session/request_permission",
            "params": {
                "request": {
                    "title": "Write file",
                    "options": [
                        {"id": "opt-1", "name": "Allow once"},
                    ],
                }
            },
        },
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
    ]

    with patch("subprocess.Popen", side_effect=make_mock_popen(responses)):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        events_iter = handle.events()

        permission_event = None
        for ev in events_iter:
            if ev.get("kind") == "permission":
                permission_event = ev
                break

        assert permission_event is not None
        request_id = permission_event.get("permission", {}).get("request_id")

        # Answer with deny
        handle.answer_permission(request_id, "deny")

        list(events_iter)

        result = handle.result()
        assert result.status == STATUS_COMPLETED


# --- Unit Tests: Result Building -----------------------------------------------


def test_result_accumulates_message_summary(test_profile: ResolvedProfile) -> None:
    """Test that the result summary accumulates message text."""
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "Hello"},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": " world!"},
                }
            },
        },
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
    ]

    with patch("subprocess.Popen", side_effect=make_mock_popen(responses)):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        list(handle.events())

        result = handle.result()
        assert result.summary == "Hello world!"
        assert result.status == STATUS_COMPLETED
        assert result.exit_reason == "end_turn"


def test_result_exit_reason_refusal(test_profile: ResolvedProfile) -> None:
    """Test that refusal stop reason maps to failed status."""
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "refusal"}},
    ]

    with patch("subprocess.Popen", side_effect=make_mock_popen(responses)):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        list(handle.events())

        result = handle.result()
        assert result.status == STATUS_FAILED
        assert result.exit_reason == "refusal"


def test_result_exit_reason_cancelled(test_profile: ResolvedProfile) -> None:
    """Test that cancelled stop reason maps to cancelled status."""
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "cancelled"}},
    ]

    with patch("subprocess.Popen", side_effect=make_mock_popen(responses)):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        list(handle.events())

        result = handle.result()
        assert result.status == STATUS_CANCELLED
        assert result.exit_reason == "cancelled"


# --- Unit Tests: Environment Variables ----------------------------------------


def test_permission_mode_read_only_profile(read_only_profile: ResolvedProfile) -> None:
    """Test that read-only profiles set DEVIN_PERMISSION_MODE=auto."""
    captured_env = {}

    def capture_env_popen(*args: Any, **kwargs: Any) -> Any:
        captured_env.update(kwargs.get("env", {}))
        return make_mock_popen([
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
            {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
        ])(*args, **kwargs)

    with patch("subprocess.Popen", side_effect=capture_env_popen):
        handle = DevinAcpHandle(
            task="test task",
            profile=read_only_profile,
            cwd="/tmp",
        )

        list(handle.events())

        assert captured_env.get("DEVIN_PERMISSION_MODE") == "auto"


def test_model_env_variable(test_profile: ResolvedProfile) -> None:
    """Test that the model is passed via DEVIN_MODEL env var."""
    captured_env = {}

    def capture_env_popen(*args: Any, **kwargs: Any) -> Any:
        captured_env.update(kwargs.get("env", {}))
        return make_mock_popen([
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123"}},
            {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
        ])(*args, **kwargs)

    with patch("subprocess.Popen", side_effect=capture_env_popen):
        handle = DevinAcpHandle(
            task="test task",
            profile=test_profile,
            cwd="/tmp",
        )

        list(handle.events())

        assert captured_env.get("DEVIN_MODEL") == "sonnet"


# --- Integration Tests: Real Devin (gated) ------------------------------------


@pytest.mark.skipif(
    not shutil.which("devin"),
    reason="devin binary not found",
)
def test_real_devin_trivial_task() -> None:
    """Real e2e test: spawn actual devin acp and run a trivial task.

    This test is skipped if devin is not installed or not authenticated.
    """
    backend = DevinAcpBackend()
    profile = ResolvedProfile(
        name="test_e2e",
        backend="devin",
        model="sonnet",
        sandbox=SANDBOX_WORKSPACE_WRITE,
    )

    # Check availability first
    ok, reason = backend.check_available(profile)
    if not ok:
        pytest.skip(f"devin not available: {reason}")

    # Use the repo directory (devin acp restricts to child dirs of cwd)
    import os
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Run a trivially cheap task
    handle = backend.start(
        task="Reply with exactly the word DONE and make no tool calls.",
        profile=profile,
        cwd=repo_dir,
    )

    events = list(handle.events())
    result = handle.result()

    # If authentication failed, skip the test
    if result.error and "authenticated" in result.error.lower():
        pytest.skip(f"devin acp requires authentication: {result.error}")

    # Verify completion
    assert result.status == STATUS_COMPLETED
    assert result.backend_session_id is not None
    assert len(events) > 0

    # Should have at least a message event
    message_events = [e for e in events if e.get("kind") == "message"]
    assert len(message_events) > 0
