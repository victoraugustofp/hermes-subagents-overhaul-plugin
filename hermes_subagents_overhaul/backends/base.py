"""Backend abstraction — the contract every subagent backend implements.

A *backend* knows how to spawn and drive one kind of external AI-agent process
(Devin via ``devin acp``; OpenAI Codex via ``codex app-server``). The
:class:`~hermes_subagents_overhaul.manager.SubagentManager` treats all backends
uniformly through these protocols, so it never imports a concrete backend.

Normalized events
-----------------
Backends translate their native event streams into :data:`SubagentEvent` dicts.
The manager forwards these to a :class:`~hermes_subagents_overhaul.sinks.base.SubagentSink`
(ACP or CLI progress). Keeping events backend-neutral is what lets the same sink
render Devin and Codex activity identically.

Permission rendezvous (foreground)
-----------------------------------
When a backend needs human approval it yields a ``kind="permission"`` event whose
``permission`` payload carries a ``request_id`` and the available ``options``. The
consumer (manager) resolves it via the sink and **must** call
:meth:`SubagentHandle.answer_permission` with that ``request_id`` to unblock the
child. Backends therefore park the child's pending request until answered (or until
cancelled / the run is non-interactive, in which case the backend applies its
profile-default policy).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, TypedDict, runtime_checkable

from hermes_subagents_overhaul.config import ResolvedProfile


class SubagentEvent(TypedDict, total=False):
    """A normalized activity event from a running subagent.

    ``kind`` is always present; all other keys are optional and depend on ``kind``.

    kind values
    -----------
    - ``"message"``    : assistant text          -> ``text``
    - ``"thought"``    : reasoning text          -> ``text``
    - ``"tool_call"``  : child tool started      -> ``child_id``, ``title``, ``tool_kind``, ``status``, ``raw_input``, ``locations``
    - ``"tool_update"``: child tool progressed   -> ``child_id``, ``status``, ``content``, ``raw_output``
    - ``"diff"``       : file edit               -> ``diff`` = {``path``, ``old_text``, ``new_text``}
    - ``"permission"`` : approval needed         -> ``permission`` = {``request_id``, ``title``, ``options``, ``tool_call``}
    - ``"plan"``       : plan/todo list          -> ``plan`` (full entry list)
    - ``"usage"``      : token accounting        -> ``usage`` = {``input_tokens``, ``output_tokens``, ...}
    - ``"status"``     : lifecycle note          -> ``text``
    - ``"done"``       : terminal                -> handled via :meth:`SubagentHandle.result`
    - ``"error"``      : terminal error          -> ``error``
    """

    kind: str
    text: str
    title: str
    child_id: str
    tool_kind: str          # ACP kind: read|edit|delete|move|search|execute|think|fetch|other
    status: str             # pending|in_progress|completed|failed
    content: list[dict[str, Any]]
    locations: list[dict[str, Any]]
    raw_input: Any
    raw_output: Any
    diff: dict[str, Any]
    permission: dict[str, Any]
    plan: list[Any]
    usage: dict[str, Any]
    error: str
    raw: dict[str, Any]     # original backend payload (debugging)


def event(kind: str, **fields: Any) -> SubagentEvent:
    """Construct a :data:`SubagentEvent` with ``kind`` and any extra fields."""
    ev: SubagentEvent = {"kind": kind}  # type: ignore[assignment]
    for k, v in fields.items():
        if v is not None:
            ev[k] = v  # type: ignore[literal-required]
    return ev


# Terminal statuses for a subagent run.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_ERROR = "error"


@dataclass
class SubagentResult:
    """The final outcome of a subagent run."""

    status: str = STATUS_COMPLETED          # completed|failed|cancelled|error
    summary: str = ""                        # final assistant text / summary
    files_changed: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    backend_session_id: str | None = None    # child thread/session id (for resume)
    exit_reason: str | None = None           # end_turn|interrupted|timeout|error|...
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "summary": self.summary}
        if self.files_changed:
            d["files_changed"] = list(self.files_changed)
        if self.usage:
            d["usage"] = dict(self.usage)
        if self.exit_reason:
            d["exit_reason"] = self.exit_reason
        if self.error:
            d["error"] = self.error
        return d


@runtime_checkable
class SubagentHandle(Protocol):
    """A running (or finished) subagent process."""

    id: str                       # our agent_id, e.g. "sa_codex_ab12cd"
    backend_session_id: str | None  # child's own session/thread id (for resume)

    def events(self) -> Iterator[SubagentEvent]:
        """Yield :data:`SubagentEvent`s, blocking, until the run terminates."""
        ...

    def answer_permission(self, request_id: str, outcome: str, option_id: str | None = None) -> None:
        """Unblock a pending ``kind="permission"`` event.

        ``outcome`` is one of ``"allow"`` / ``"deny"`` (or a backend-specific
        decision); ``option_id`` selects a specific ACP permission option when one
        was offered.
        """
        ...

    def cancel(self) -> None:
        """Request cancellation (interrupt the child / kill the subprocess)."""
        ...

    def result(self) -> SubagentResult:
        """Return the final result. Valid once :meth:`events` has terminated."""
        ...


@runtime_checkable
class SubagentBackend(Protocol):
    """Spawns and configures one kind of external subagent process."""

    name: str  # "codex" | "devin"

    def check_available(self, profile: ResolvedProfile) -> tuple[bool, str]:
        """Return ``(ok, reason)``. ``ok=False`` -> the tool/profile is gated off."""
        ...

    def start(
        self,
        *,
        task: str,
        profile: ResolvedProfile,
        cwd: str,
        resume_handle: str | None = None,
    ) -> SubagentHandle:
        """Spawn the child for ``task`` and return a :class:`SubagentHandle`."""
        ...


def new_agent_id(backend: str) -> str:
    """Stable, readable id used as the ACP umbrella ``toolCallId`` for a subagent."""
    return f"sa_{backend}_{secrets.token_hex(3)}"
