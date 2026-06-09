"""Shared test fixtures: HERMES_HOME isolation, fake backends/handles, a recording
sink, and a recording PluginContext. No external processes are spawned here — the
real-process backend tests live in their own files and gate on the binaries.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Callable, Iterator

import pytest

from hermes_subagents_overhaul import config as _config
from hermes_subagents_overhaul.backends.base import (
    STATUS_COMPLETED,
    SubagentEvent,
    SubagentResult,
    event,
    new_agent_id,
)
from hermes_subagents_overhaul.manager import SubagentManager
from hermes_subagents_overhaul.sinks.base import OUTCOME_DENY, SubagentSink


@pytest.fixture(autouse=True)
def _isolate_hermes_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("HERMES_HOME", tmp)
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        yield


# --- fakes -------------------------------------------------------------------
class FakeHandle:
    """A scripted :class:`SubagentHandle`. ``script`` is a list of SubagentEvents;
    a ``kind="permission"`` event pauses until :meth:`answer_permission`."""

    def __init__(
        self,
        agent_id: str,
        script: list[SubagentEvent],
        result: SubagentResult,
        *,
        backend_session_id: str | None = None,
    ) -> None:
        self.id = agent_id
        self.backend_session_id = backend_session_id
        self._script = script
        self._result = result
        self._answers: dict[str, tuple[str, str | None]] = {}
        self.cancelled = False
        self.permission_outcomes: list[tuple[str, str | None]] = []

    def events(self) -> Iterator[SubagentEvent]:
        for ev in self._script:
            if self.cancelled:
                return
            yield ev
            if ev.get("kind") == "permission":
                rid = str((ev.get("permission") or {}).get("request_id") or "")
                self.permission_outcomes.append(self._answers.get(rid, (OUTCOME_DENY, None)))

    def answer_permission(self, request_id: str, outcome: str, option_id: str | None = None) -> None:
        self._answers[request_id] = (outcome, option_id)

    def cancel(self) -> None:
        self.cancelled = True

    def result(self) -> SubagentResult:
        return self._result


class FakeBackend:
    """A :class:`SubagentBackend` returning :class:`FakeHandle`s.

    ``script_factory(task, profile, resume_handle) -> (script, result)`` lets each
    test shape the event stream. ``available`` gates ``check_available``.
    """

    def __init__(
        self,
        name: str = "codex",
        *,
        available: bool = True,
        script_factory: Callable[..., tuple[list[SubagentEvent], SubagentResult]] | None = None,
    ) -> None:
        self.name = name
        self._available = available
        self._script_factory = script_factory or self._default_script
        self.started: list[dict[str, Any]] = []

    @staticmethod
    def _default_script(task: str, profile: Any, resume_handle: str | None):
        script: list[SubagentEvent] = [
            event("thought", text="thinking"),
            event("tool_call", child_id="t1", title="read file", tool_kind="read", status="pending"),
            event("tool_update", child_id="t1", status="completed"),
            event("message", text=f"done: {task[:40]}"),
        ]
        result = SubagentResult(
            status=STATUS_COMPLETED,
            summary=f"completed: {task[:60]}",
            backend_session_id="thread-xyz",
            exit_reason="end_turn",
        )
        return script, result

    def check_available(self, profile: Any) -> tuple[bool, str]:
        return (self._available, "" if self._available else "fake backend disabled")

    def start(self, *, task: str, profile: Any, cwd: str, resume_handle: str | None = None):
        self.started.append({"task": task, "profile": profile, "cwd": cwd, "resume": resume_handle})
        script, result = self._script_factory(task, profile, resume_handle)
        return FakeHandle(new_agent_id(self.name), script, result,
                          backend_session_id=result.backend_session_id)


class RecordingSink:
    """Records sink calls; ``permission_outcome`` is returned from request_permission."""

    def __init__(self, agent_id: str, *, background: bool = False, permission_outcome: str = OUTCOME_DENY):
        self.agent_id = agent_id
        self.background = background
        self.permission_outcome = permission_outcome
        self.started: dict[str, Any] | None = None
        self.events: list[SubagentEvent] = []
        self.permissions: list[dict[str, Any]] = []
        self.result: SubagentResult | None = None

    def start(self, **kw: Any) -> None:
        self.started = kw

    def event(self, ev: SubagentEvent) -> None:
        self.events.append(ev)

    def request_permission(self, permission: dict[str, Any]) -> str:
        self.permissions.append(permission)
        return self.permission_outcome

    def done(self, result: SubagentResult) -> None:
        self.result = result


class RecordingCtx:
    """A stand-in for Hermes' PluginContext that records registrations."""

    def __init__(self) -> None:
        self.tools: dict[str, dict[str, Any]] = {}
        self.hooks: dict[str, list[Callable]] = {}
        self.commands: dict[str, dict[str, Any]] = {}
        self.injected: list[tuple[str, str]] = []

    def register_tool(self, *, name: str, toolset: str, schema: dict, handler: Callable, **kw: Any) -> None:
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kw}

    def register_hook(self, name: str, cb: Callable) -> None:
        self.hooks.setdefault(name, []).append(cb)

    def register_command(self, name: str, handler: Callable, description: str = "", args_hint: str = "") -> None:
        self.commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}

    def register_skill(self, name: str, path: str) -> None:  # pragma: no cover - unused here
        pass

    def inject_message(self, content: str, role: str = "user") -> bool:
        self.injected.append((role, content))
        return True

    def dispatch_tool(self, name: str, args: dict, **kw: Any):  # pragma: no cover - unused here
        return self.tools[name]["handler"](args, **kw)


# --- fixtures ----------------------------------------------------------------
@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend(name="codex")


@pytest.fixture
def recording_sinks() -> list[RecordingSink]:
    return []


@pytest.fixture
def make_manager(fake_backend: FakeBackend, recording_sinks: list[RecordingSink]):
    def _factory(*, cfg: dict[str, Any] | None = None, permission_outcome: str = OUTCOME_DENY,
                 backends: dict[str, Any] | None = None) -> SubagentManager:
        def sink_factory(agent_id: str, *, background: bool = False, **kw: Any) -> SubagentSink:
            s = RecordingSink(agent_id, background=background, permission_outcome=permission_outcome)
            recording_sinks.append(s)
            return s

        cfg = cfg if cfg is not None else _config.load_config({})
        return SubagentManager(
            backends=backends or {fake_backend.name: fake_backend},
            cfg=cfg,
            sink_factory=sink_factory,
        )

    return _factory


@pytest.fixture
def ctx() -> RecordingCtx:
    return RecordingCtx()
