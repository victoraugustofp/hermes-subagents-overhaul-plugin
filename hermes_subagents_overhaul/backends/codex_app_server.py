"""Codex backend — drives ``codex app-server`` (native JSON-RPC over stdio).

Reuses Hermes' existing client (CodexAppServerSession) to spawn and drive a
Codex thread. Translates native codex notifications into normalized SubagentEvent
dicts and bridges approval requests via a permission rendezvous.

Threading model:
  - start() spawns a worker thread that calls session.ensure_started() + run_turn()
  - on_event callback translates each notification into SubagentEvent and pushes to queue
  - events() yields from the queue until a sentinel object signals completion
  - approval_callback blocks the worker thread; answer_permission() releases it
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import uuid
from typing import Any, Callable, Iterator, Optional

from agent.transports.codex_app_server import CodexAppServerClient
from agent.transports.codex_app_server_session import CodexAppServerSession, TurnResult

from hermes_subagents_overhaul.backends.base import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SubagentEvent,
    SubagentHandle,
    SubagentResult,
    event,
    new_agent_id,
)
from hermes_subagents_overhaul.config import ResolvedProfile


# Sentinel object to signal end of event stream
_STREAM_END = object()


def _make_session(
    cwd: str,
    profile: ResolvedProfile,
    approval_callback: Callable[[str, str, bool], str],
    on_event: Callable[[dict], None],
) -> CodexAppServerSession:
    """Factory for CodexAppServerSession. Allows tests to inject a fake."""
    # Build extra_args from profile
    extra_args: list[str] = []
    if profile.model:
        extra_args.extend(["-c", f'model="{profile.model}"'])
    if profile.sandbox:
        extra_args.extend(["-c", f'sandbox_mode="{profile.sandbox}"'])
    extra_args.extend(profile.extra_args)

    # Merge env
    env = dict(os.environ)
    env.update(profile.env)

    # Create a client_factory that passes extra_args
    def client_factory(codex_bin: str = "codex", codex_home: Optional[str] = None):
        return CodexAppServerClient(
            codex_bin=codex_bin,
            codex_home=codex_home,
            extra_args=extra_args if extra_args else None,
            env=env if env else None,
        )

    return CodexAppServerSession(
        cwd=cwd,
        approval_callback=approval_callback,
        on_event=on_event,
        client_factory=client_factory,
    )


class CodexAppServerHandle:
    """A running Codex subagent process."""

    def __init__(
        self,
        agent_id: str,
        session: CodexAppServerSession,
        worker_thread: threading.Thread,
        event_queue: queue.Queue,
        permission_waiters: dict[str, threading.Event],
        permission_answers: dict[str, tuple[str, str | None]],
    ) -> None:
        self.id = agent_id
        self.backend_session_id: str | None = None
        self._session = session
        self._worker_thread = worker_thread
        self._event_queue = event_queue
        self._permission_waiters = permission_waiters
        self._permission_answers = permission_answers
        self._cancelled = False
        self._result: SubagentResult | None = None

    def events(self) -> Iterator[SubagentEvent]:
        """Yield SubagentEvents until the stream ends."""
        while True:
            try:
                item = self._event_queue.get(timeout=0.5)
            except queue.Empty:
                # Check if worker is still alive
                if not self._worker_thread.is_alive():
                    # Drain remaining items
                    try:
                        while True:
                            item = self._event_queue.get_nowait()
                            if item is _STREAM_END:
                                return
                            yield item  # type: ignore[misc]
                    except queue.Empty:
                        return
                continue

            if item is _STREAM_END:
                return
            yield item  # type: ignore[misc]

    def answer_permission(
        self, request_id: str, outcome: str, option_id: str | None = None
    ) -> None:
        """Unblock a pending permission request."""
        self._permission_answers[request_id] = (outcome, option_id)
        if request_id in self._permission_waiters:
            self._permission_waiters[request_id].set()

    def cancel(self) -> None:
        """Request cancellation."""
        self._cancelled = True
        try:
            self._session.request_interrupt()
        except Exception:
            pass
        try:
            self._session.close()
        except Exception:
            pass

    def result(self) -> SubagentResult:
        """Return the final result. Valid once events() has terminated."""
        if self._result is not None:
            return self._result
        # Wait for worker to finish
        self._worker_thread.join(timeout=5.0)
        # Should have been set by worker; fallback if not
        return self._result or SubagentResult(
            status=STATUS_FAILED,
            summary="",
            error="Worker thread did not set result",
        )


class CodexAppServerBackend:
    """Spawns and drives Codex via app-server."""

    name = "codex"

    def check_available(self, profile: ResolvedProfile) -> tuple[bool, str]:
        """Check if codex binary exists and credentials are available."""
        # Check for codex binary
        if not shutil.which("codex"):
            return (False, "codex binary not found in PATH")

        # Check for credentials: OPENAI_API_KEY env or ~/.codex/auth.json
        if os.environ.get("OPENAI_API_KEY"):
            return (True, "")

        codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        auth_json = os.path.join(codex_home, "auth.json")
        if os.path.isfile(auth_json):
            return (True, "")

        return (
            False,
            "No Codex credentials found. Set OPENAI_API_KEY or create ~/.codex/auth.json",
        )

    def start(
        self,
        *,
        task: str,
        profile: ResolvedProfile,
        cwd: str,
        resume_handle: str | None = None,
    ) -> SubagentHandle:
        """Spawn a Codex subagent and return a handle."""
        agent_id = new_agent_id("codex")
        event_queue: queue.Queue = queue.Queue()
        permission_waiters: dict[str, threading.Event] = {}
        permission_answers: dict[str, tuple[str, str | None]] = {}
        turn_result: TurnResult | None = None

        def on_event(notification: dict) -> None:
            """Translate codex notification to SubagentEvent and enqueue."""
            try:
                _translate_notification(notification, event_queue, agent_id)
            except Exception:
                # Never raise from on_event callback
                pass

        def approval_callback(
            command_label: str, description: str, allow_permanent: bool
        ) -> str:
            """Bridge approval request to the manager via permission event."""
            request_id = str(uuid.uuid4())
            permission_waiters[request_id] = threading.Event()

            # Enqueue permission event
            perm_event = event(
                "permission",
                permission={
                    "request_id": request_id,
                    "title": command_label or description,
                    "options": [
                        {"id": "allow", "name": "Allow"},
                        {"id": "deny", "name": "Deny"},
                    ],
                    "tool_call": {},  # Placeholder
                },
            )
            event_queue.put(perm_event)

            # Block until answer_permission is called
            permission_waiters[request_id].wait(timeout=600.0)

            # Map outcome to codex decision string
            outcome, option_id = permission_answers.get(request_id, ("deny", None))
            if outcome in ("allow", "once"):
                return "once"
            elif outcome in ("session", "always"):
                return "acceptForSession"
            else:
                return "deny"

        # Create session
        session = _make_session(cwd, profile, approval_callback, on_event)

        # Create handle
        handle = CodexAppServerHandle(
            agent_id,
            session,
            threading.Thread(),  # Placeholder, will be set below
            event_queue,
            permission_waiters,
            permission_answers,
        )

        # Worker thread that runs the turn
        def worker() -> None:
            nonlocal turn_result
            try:
                thread_id = session.ensure_started()
                handle.backend_session_id = thread_id
            except Exception as e:
                turn_result = TurnResult(
                    error=f"Failed to start codex: {e}",
                    should_retire=True,
                )
                event_queue.put(_STREAM_END)
                return

            try:
                turn_result = session.run_turn(task, turn_timeout=600.0)
            except Exception as e:
                turn_result = TurnResult(
                    error=f"Codex turn failed: {e}",
                    should_retire=True,
                )
            finally:
                event_queue.put(_STREAM_END)

        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()
        handle._worker_thread = worker_thread

        # Set result builder
        def set_result() -> None:
            """Called after events() ends to build final result."""
            if turn_result is None:
                handle._result = SubagentResult(
                    status=STATUS_FAILED,
                    summary="",
                    error="No turn result",
                )
                return

            # Determine status
            if turn_result.error:
                status = STATUS_FAILED
            elif turn_result.interrupted:
                status = STATUS_FAILED
            elif turn_result.should_retire:
                status = STATUS_FAILED
            else:
                status = STATUS_COMPLETED

            # Collect files_changed from projected_messages (not directly available)
            files_changed: list[str] = []

            # Build result
            handle._result = SubagentResult(
                status=status,
                summary=turn_result.final_text or "",
                files_changed=files_changed,
                backend_session_id=turn_result.thread_id,
                exit_reason=(
                    "interrupted" if turn_result.interrupted
                    else "error" if turn_result.error
                    else "end_turn"
                ),
                error=turn_result.error,
            )

        # Wrap events() to set result when stream ends
        original_events = handle.events

        def events_with_result() -> Iterator[SubagentEvent]:
            try:
                yield from original_events()
            finally:
                set_result()

        handle.events = events_with_result  # type: ignore[assignment]

        return handle


def _translate_notification(
    notification: dict, event_queue: queue.Queue, agent_id: str
) -> None:
    """Translate a codex notification into SubagentEvent(s) and enqueue them."""
    method = notification.get("method", "")
    params = notification.get("params") or {}

    if method == "item/started":
        _handle_item_started(params, event_queue)
    elif method == "item/completed":
        _handle_item_completed(params, event_queue)
    elif method == "turn/completed":
        _handle_turn_completed(params, event_queue)
    elif method == "thread/tokenUsage/updated":
        _handle_token_usage(params, event_queue)
    # Ignore other notifications (turn/started, item/*/delta, etc.)


def _handle_item_started(params: dict, event_queue: queue.Queue) -> None:
    """Handle item/started notification."""
    item = params.get("item") or {}
    item_type = item.get("type", "")
    item_id = item.get("id", "")

    # Map item types to tool_kind
    tool_kind_map = {
        "commandExecution": "execute",
        "fileChange": "edit",
        "mcpToolCall": "other",
        "dynamicToolCall": "other",
    }

    if item_type in tool_kind_map:
        ev = event(
            "tool_call",
            child_id=item_id,
            title=item_type,
            tool_kind=tool_kind_map[item_type],
            status="pending",
            raw_input=item,
        )
        event_queue.put(ev)


def _handle_item_completed(params: dict, event_queue: queue.Queue) -> None:
    """Handle item/completed notification."""
    item = params.get("item") or {}
    item_type = item.get("type", "")
    item_id = item.get("id", "")

    if item_type == "agentMessage":
        text = item.get("text", "")
        if text:
            ev = event("message", text=text)
            event_queue.put(ev)

    elif item_type == "reasoning":
        # Combine summary and content
        summary_parts = item.get("summary") or []
        content_parts = item.get("content") or []
        text = "\n".join(str(p) for p in summary_parts + content_parts if p)
        if text:
            ev = event("thought", text=text)
            event_queue.put(ev)

    elif item_type == "commandExecution":
        # Emit tool_update for completed command
        status_str = item.get("status", "")
        status = "completed" if status_str == "completed" else "failed"
        output = item.get("aggregatedOutput", "")
        # Truncate to 4000 chars
        output = output[:4000] if output else ""
        content = [{"type": "text", "text": output}] if output else []

        ev = event(
            "tool_update",
            child_id=item_id,
            status=status,
            content=content,
        )
        event_queue.put(ev)

    elif item_type == "fileChange":
        # Emit diff events for each change
        changes = item.get("changes") or []
        for change in changes:
            path = change.get("path", "")
            kind = (change.get("kind") or {}).get("type", "update")
            before = change.get("before", "")
            after = change.get("after", "")

            diff_ev = event(
                "diff",
                child_id=item_id,
                diff={
                    "path": path,
                    "old_text": before or "",
                    "new_text": after or "",
                },
            )
            event_queue.put(diff_ev)

        # Also emit tool_update for the fileChange completion
        ev = event(
            "tool_update",
            child_id=item_id,
            status="completed",
            content=[{"type": "text", "text": f"Applied {len(changes)} file change(s)"}],
        )
        event_queue.put(ev)

    elif item_type in ("mcpToolCall", "dynamicToolCall"):
        # Emit tool_update for tool completion
        status_str = item.get("status", "")
        status = "completed" if status_str == "completed" else "failed"
        result = item.get("result") or item.get("error") or ""
        content = [{"type": "text", "text": str(result)[:4000]}] if result else []

        ev = event(
            "tool_update",
            child_id=item_id,
            status=status,
            content=content,
        )
        event_queue.put(ev)


def _handle_turn_completed(params: dict, event_queue: queue.Queue) -> None:
    """Handle turn/completed notification."""
    # The turn completion is handled by the worker thread detecting
    # the TurnResult. We don't need to emit an event here.
    pass


def _handle_token_usage(params: dict, event_queue: queue.Queue) -> None:
    """Handle thread/tokenUsage/updated notification."""
    usage = params.get("usage") or {}
    if usage:
        ev = event("usage", usage=usage)
        event_queue.put(ev)
