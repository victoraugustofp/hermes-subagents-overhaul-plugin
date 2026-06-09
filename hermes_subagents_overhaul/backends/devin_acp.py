"""Devin backend — spawns ``devin acp`` (native ACP server over stdio) and drives
it as a PURE ACP CLIENT.

Implements the full ACP client protocol: initialize -> session/new -> session/prompt,
translates all session/update kinds into normalized SubagentEvents, and handles
inbound session/request_permission with a rendezvous pattern.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from hermes_subagents_overhaul import binaries
from hermes_subagents_overhaul.backends.base import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    SubagentEvent,
    SubagentResult,
    event,
    new_agent_id,
)
from hermes_subagents_overhaul.config import ResolvedProfile, SANDBOX_READ_ONLY


def _resolve_devin_api_key() -> str | None:
    """The windsurf API key for ACP `authenticate` (method ``windsurf-api-key``).

    `devin acp` ignores local CLI creds at runtime, so the host must supply the key.
    Source order: ``WINDSURF_API_KEY`` env, then ``windsurf_api_key`` in the on-disk
    credential store written by ``devin auth login``.
    """
    env_key = os.getenv("WINDSURF_API_KEY")
    if env_key:
        return env_key
    creds = Path(os.path.expanduser("~")) / ".local" / "share" / "devin" / "credentials.toml"
    if not creds.is_file():
        return None
    try:
        import tomllib  # py3.11+

        with open(creds, "rb") as fh:
            val = tomllib.load(fh).get("windsurf_api_key")
        if val:
            return str(val)
    except Exception:
        try:  # minimal fallback for `windsurf_api_key = "..."`
            for line in creds.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("windsurf_api_key"):
                    return s.partition("=")[2].strip().strip('"').strip("'") or None
        except Exception:
            return None
    return None


class DevinAcpBackend:
    """Spawns and drives ``devin acp`` as an ACP client over stdio."""

    name = "devin"

    def check_available(self, profile: ResolvedProfile) -> tuple[bool, str]:
        """Check if devin binary exists and user is logged in.

        Returns (True, "") if available, else (False, reason).
        """
        if not binaries.resolve_backend_binary("devin"):
            return (False, "devin binary not found (PATH or ~/.local/bin/Homebrew)")

        # `devin acp` mandates ACP-host authentication, so we need a usable key.
        if not _resolve_devin_api_key():
            return (
                False,
                "devin not authenticated for ACP (set WINDSURF_API_KEY or run "
                "`devin auth login` to populate windsurf_api_key)",
            )
        return (True, "")

    def start(
        self,
        *,
        task: str,
        profile: ResolvedProfile,
        cwd: str,
        resume_handle: str | None = None,
    ) -> SubagentHandle:
        """Spawn ``devin acp`` and return a handle.

        Args:
            task: The prompt/task to send to devin.
            profile: The resolved profile (model, sandbox, permission_mode, env).
            cwd: Working directory for the child process.
            resume_handle: If provided, attempt to resume a prior session.

        Returns:
            A SubagentHandle that yields events and manages the child process.
        """
        return DevinAcpHandle(
            task=task,
            profile=profile,
            cwd=cwd,
            resume_handle=resume_handle,
        )


class DevinAcpHandle:
    """A running devin acp process, driven as an ACP client over stdio."""

    def __init__(
        self,
        task: str,
        profile: ResolvedProfile,
        cwd: str,
        resume_handle: str | None = None,
    ) -> None:
        self.id = new_agent_id("devin")
        self.backend_session_id: str | None = None
        self._task = task
        self._profile = profile
        self._cwd = cwd
        self._resume_handle = resume_handle

        # Event queue and state
        self._event_queue: queue.Queue[SubagentEvent | None] = queue.Queue()
        self._permission_waiters: dict[str, threading.Event] = {}
        self._permission_outcomes: dict[str, tuple[str, str | None]] = {}
        self._cancelled = False
        self._result: SubagentResult | None = None

        # Spawn the child and run the ACP protocol
        self._proc: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._start_acp_session()

    def _start_acp_session(self) -> None:
        """Spawn devin acp, run the ACP handshake, and start the reader thread."""
        # Build the environment
        env = os.environ.copy()
        env.update(self._profile.env)

        # Set model if specified
        if self._profile.model:
            env["DEVIN_MODEL"] = self._profile.model

        # Set permission mode: read-only profiles use "auto", others use "dangerous"
        if self._profile.read_only or self._profile.sandbox == SANDBOX_READ_ONLY:
            env["DEVIN_PERMISSION_MODE"] = "auto"
        elif self._profile.permission_mode:
            env["DEVIN_PERMISSION_MODE"] = self._profile.permission_mode
        else:
            env["DEVIN_PERMISSION_MODE"] = "dangerous"

        # Resolve the devin binary to an absolute path (the inherited PATH may be
        # minimal under a GUI-launched ACP server) and ensure the child's PATH
        # includes its dir + common bin dirs so `devin acp` can find node etc.
        devin_bin = binaries.resolve_backend_binary("devin") or "devin"
        env = binaries.child_env_with_resolved_path(devin_bin, env)

        # Spawn the child
        try:
            self._proc = subprocess.Popen(
                [devin_bin, "acp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self._cwd,
                env=env,
            )
        except FileNotFoundError as exc:
            self._result = SubagentResult(
                status=STATUS_FAILED,
                error=f"Could not spawn devin acp: {exc}",
            )
            self._event_queue.put(None)  # Signal end
            return

        if self._proc.stdin is None or self._proc.stdout is None:
            self._proc.kill()
            self._result = SubagentResult(
                status=STATUS_FAILED,
                error="devin acp process did not expose stdin/stdout pipes",
            )
            self._event_queue.put(None)
            return

        # Start the reader thread
        self._reader_thread = threading.Thread(
            target=self._run_acp_protocol,
            daemon=True,
        )
        self._reader_thread.start()

    def _run_acp_protocol(self) -> None:
        """Run the ACP protocol: initialize -> session/new -> session/prompt."""
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            return

        inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        next_id = [0]  # Use list to allow mutation in nested function

        def _stdout_reader() -> None:
            """Background thread that reads stdout and parses JSON-RPC messages."""
            if self._proc is None or self._proc.stdout is None:
                return
            try:
                # Handle both real file objects and mock iterables
                stdout = self._proc.stdout
                if hasattr(stdout, '__iter__'):
                    for line in stdout:
                        if line is None:
                            break
                        try:
                            msg = json.loads(line)
                            inbox.put(msg)
                        except (json.JSONDecodeError, TypeError):
                            # Ignore malformed lines
                            pass
            except Exception:
                pass

        reader_thread = threading.Thread(target=_stdout_reader, daemon=True)
        reader_thread.start()

        def _request(method: str, params: dict[str, Any]) -> Any:
            """Send a JSON-RPC request and wait for the response."""
            nonlocal next_id
            next_id[0] += 1
            request_id = next_id[0]

            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()

            # Wait for the response (with timeout)
            deadline = time.monotonic() + 60.0  # 60s timeout per request
            while time.monotonic() < deadline:
                if self._cancelled or self._proc.poll() is not None:
                    raise RuntimeError(f"Process terminated while waiting for {method}")

                try:
                    msg = inbox.get(timeout=0.1)
                except queue.Empty:
                    continue

                # Handle server-initiated messages (notifications)
                if self._handle_server_message(msg):
                    continue

                # Check if this is our response
                if msg.get("id") != request_id:
                    continue

                if "error" in msg:
                    err = msg.get("error") or {}
                    raise RuntimeError(
                        f"devin acp {method} failed: {err.get('message') or err}"
                    )

                return msg.get("result")

            raise TimeoutError(f"Timed out waiting for devin acp response to {method}")

        try:
            # ACP handshake
            init_result = _request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {
                            "readTextFile": True,
                            "writeTextFile": True,
                        }
                    },
                    "clientInfo": {
                        "name": "hermes-subagent",
                        "title": "Hermes Subagent",
                        "version": "1.0.0",
                    },
                },
            )

            # Authenticate. `devin acp` is the sole-credential model: it ignores
            # local CLI creds and *requires* the ACP host to call `authenticate`
            # with the API key. The correct ACP shape is
            #   {"methodId": <advertised id>, "_meta": {"api_key": <key>}}
            # (verified against devin 2026.5.26-7). The key comes from env or the
            # on-disk credential store written by `devin auth login`.
            auth_methods = (init_result or {}).get("authMethods") or []
            if auth_methods:
                method_id = str(auth_methods[0].get("id") or "windsurf-api-key")
                api_key = _resolve_devin_api_key()
                if api_key:
                    _request(
                        "authenticate",
                        {"methodId": method_id, "_meta": {"api_key": api_key}},
                    )

            # Create or resume session
            if self._resume_handle:
                # Try to load a prior session
                try:
                    session = _request(
                        "session/load",
                        {
                            "sessionId": self._resume_handle,
                            "cwd": self._cwd,
                        },
                    ) or {}
                    self.backend_session_id = str(session.get("sessionId") or "").strip()
                except Exception:
                    # Fall back to a fresh session
                    session = _request(
                        "session/new",
                        {
                            "cwd": self._cwd,
                            "mcpServers": [],
                        },
                    ) or {}
                    self.backend_session_id = str(session.get("sessionId") or "").strip()
            else:
                session = _request(
                    "session/new",
                    {
                        "cwd": self._cwd,
                        "mcpServers": [],
                    },
                ) or {}
                self.backend_session_id = str(session.get("sessionId") or "").strip()

            if not self.backend_session_id:
                raise RuntimeError("devin acp did not return a sessionId")

            # Send the prompt
            response = _request(
                "session/prompt",
                {
                    "sessionId": self.backend_session_id,
                    "prompt": [
                        {
                            "type": "text",
                            "text": self._task,
                        }
                    ],
                },
            ) or {}

            # Extract stop reason
            stop_reason = str(response.get("stopReason") or "").strip() or "end_turn"

            # Build the final result
            self._result = SubagentResult(
                status=STATUS_COMPLETED
                if stop_reason not in {"refusal", "cancelled"}
                else (STATUS_CANCELLED if stop_reason == "cancelled" else STATUS_FAILED),
                summary="",  # Will be filled from accumulated messages
                backend_session_id=self.backend_session_id,
                exit_reason=stop_reason,
            )

        except Exception as exc:
            self._result = SubagentResult(
                status=STATUS_FAILED,
                error=str(exc),
                backend_session_id=self.backend_session_id,
            )
        finally:
            # Signal end of events
            self._event_queue.put(None)
            # Clean up the process
            if self._proc:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass

    def _handle_server_message(self, msg: dict[str, Any]) -> bool:
        """Handle server-initiated messages (notifications).

        Returns True if the message was handled (and should not be treated as a response).
        """
        method = msg.get("method")
        if not isinstance(method, str):
            return False

        if method == "session/update":
            self._handle_session_update(msg)
            return True

        if method == "session/request_permission":
            self._handle_request_permission(msg)
            return True

        return False

    def _handle_session_update(self, msg: dict[str, Any]) -> None:
        """Translate a session/update notification into SubagentEvent(s)."""
        params = msg.get("params") or {}
        update = params.get("update") or {}
        kind = str(update.get("sessionUpdate") or "").strip()

        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            text = str(content.get("text") or "")
            if text:
                self._event_queue.put(event("message", text=text))

        elif kind == "agent_thought_chunk":
            content = update.get("content") or {}
            text = str(content.get("text") or "")
            if text:
                self._event_queue.put(event("thought", text=text))

        elif kind == "tool_call":
            tool_call_id = str(update.get("toolCallId") or "").strip()
            title = str(update.get("title") or "").strip()
            tool_kind = str(update.get("kind") or "").strip()
            status = str(update.get("status") or "pending").strip()
            raw_input = update.get("rawInput")
            locations = update.get("locations") or []

            self._event_queue.put(
                event(
                    "tool_call",
                    child_id=tool_call_id,
                    title=title,
                    tool_kind=tool_kind,
                    status=status,
                    raw_input=raw_input,
                    locations=locations,
                )
            )

        elif kind == "tool_call_update":
            tool_call_id = str(update.get("toolCallId") or "").strip()
            status = str(update.get("status") or "").strip()
            content = update.get("content") or []
            raw_output = update.get("rawOutput")

            self._event_queue.put(
                event(
                    "tool_update",
                    child_id=tool_call_id,
                    status=status,
                    content=content,
                    raw_output=raw_output,
                )
            )

            # Check for diffs in content
            for item in (content if isinstance(content, list) else []):
                if isinstance(item, dict) and item.get("type") == "file_edit":
                    path = item.get("path")
                    old_text = item.get("oldText")
                    new_text = item.get("newText")
                    if path:
                        self._event_queue.put(
                            event(
                                "diff",
                                diff={
                                    "path": path,
                                    "old_text": old_text or "",
                                    "new_text": new_text or "",
                                },
                            )
                        )

        elif kind == "plan":
            entries = update.get("entries") or []
            self._event_queue.put(event("plan", plan=entries))

    def _handle_request_permission(self, msg: dict[str, Any]) -> None:
        """Handle inbound session/request_permission.

        Emit a permission event and block until answer_permission is called.
        """
        message_id = msg.get("id")
        params = msg.get("params") or {}
        request = params.get("request") or {}

        # Extract permission details
        title = str(request.get("title") or "").strip()
        options = request.get("options") or []
        tool_call = request.get("toolCall")

        # Mint a request_id (use the JSON-RPC message id)
        request_id = str(message_id)

        # Create a waiter event
        waiter = threading.Event()
        self._permission_waiters[request_id] = waiter

        # Emit the permission event
        self._event_queue.put(
            event(
                "permission",
                permission={
                    "request_id": request_id,
                    "title": title,
                    "options": options,
                    "tool_call": tool_call,
                },
            )
        )

        # Block until answer_permission is called
        waiter.wait(timeout=600)  # 10-minute timeout

        # Get the outcome
        outcome, option_id = self._permission_outcomes.get(
            request_id, ("deny", None)
        )

        # Build the response
        if outcome == "allow" and option_id:
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": option_id,
                    }
                },
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "outcome": {
                        "outcome": "cancelled",
                    }
                },
            }

        # Send the response
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write(json.dumps(response) + "\n")
                self._proc.stdin.flush()
            except Exception:
                pass

        # Clean up
        self._permission_waiters.pop(request_id, None)
        self._permission_outcomes.pop(request_id, None)

    def events(self) -> Iterator[SubagentEvent]:
        """Yield SubagentEvents until the run terminates."""
        message_parts: list[str] = []

        while True:
            ev = self._event_queue.get()
            if ev is None:
                # Sentinel: end of events
                break

            # Accumulate message text for the summary
            if ev.get("kind") == "message":
                text = ev.get("text", "")
                if text:
                    message_parts.append(text)

            yield ev

        # Update the result with the accumulated summary
        if self._result:
            self._result.summary = "".join(message_parts)

    def answer_permission(
        self, request_id: str, outcome: str, option_id: str | None = None
    ) -> None:
        """Unblock a pending permission request.

        Args:
            request_id: The request_id from the permission event.
            outcome: "allow" or "deny".
            option_id: The selected option ID (required if outcome is "allow").
        """
        self._permission_outcomes[request_id] = (outcome, option_id)
        waiter = self._permission_waiters.get(request_id)
        if waiter:
            waiter.set()

    def cancel(self) -> None:
        """Request cancellation of the subagent."""
        self._cancelled = True

        # Try to send session/cancel
        if self._proc and self._proc.stdin and self.backend_session_id:
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 99999,
                    "method": "session/cancel",
                    "params": {"sessionId": self.backend_session_id},
                }
                self._proc.stdin.write(json.dumps(payload) + "\n")
                self._proc.stdin.flush()
            except Exception:
                pass

        # Terminate the process
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

    def result(self) -> SubagentResult:
        """Return the final result. Valid once events() has terminated."""
        if self._result is None:
            return SubagentResult(
                status=STATUS_FAILED,
                error="Result not yet available",
            )
        return self._result
