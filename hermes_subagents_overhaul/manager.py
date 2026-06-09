"""SubagentManager — registry + lifecycle for external subagents.

Process-lifetime singleton (like ``delegate_tool._active_subagents``) that owns:

* a registry of :class:`SubagentRecord`s keyed by ``agent_id`` (survives turns),
* the **foreground** path: drive ``handle.events()`` synchronously to completion,
  forwarding each event to the sink and relaying permission prompts,
* the **background** path: a daemon worker thread drains events to a *durable*
  sink captured at spawn (so it keeps emitting after the turn ends), sets a
  completion event, and enqueues a wake-up notification for the parent model,
* **resume** (always foreground), **cancel**, **read**, **list**, and the
  ``max_background`` / single-foreground concurrency caps.

The manager is backend- and sink-agnostic: backends are injected as a name->backend
mapping and sinks via a factory, both overridable for tests.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from hermes_subagents_overhaul import config
from hermes_subagents_overhaul.backends.base import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_RUNNING,
    SubagentBackend,
    SubagentEvent,
    SubagentHandle,
    SubagentResult,
    new_agent_id,
)
from hermes_subagents_overhaul.sinks.base import (
    OUTCOME_ALLOW,
    OUTCOME_DENY,
    SubagentSink,
    make_sink,
)

logger = logging.getLogger("hermes_subagents_overhaul.manager")


class SubagentError(RuntimeError):
    """Tool-facing error (unknown profile, backend unavailable, cap exceeded, ...)."""


@dataclass
class SubagentRecord:
    agent_id: str
    backend_name: str
    profile_name: str
    title: str
    background: bool
    status: str = STATUS_RUNNING
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    last_activity: str = ""
    handle: SubagentHandle | None = None
    sink: SubagentSink | None = None
    result: SubagentResult | None = None
    completion_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    session_id: str | None = None     # owning ACP/CLI session, for cancel_all
    notified: bool = False            # wake-up notification already drained?

    def elapsed_s(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return round(end - self.started_at, 2)

    def public_state(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "agent_id": self.agent_id,
            "status": self.status,
            "backend": self.backend_name,
            "profile": self.profile_name,
            "title": self.title,
            "background": self.background,
            "elapsed_s": self.elapsed_s(),
        }
        if self.last_activity:
            d["last_activity"] = self.last_activity
        if self.result is not None:
            d.update(self.result.to_dict())
        return d


def _resolve_cwd(cfg: dict[str, Any]) -> str:
    ws = cfg.get("workspace") or "auto"
    if ws and ws != "auto":
        return str(ws)
    return os.environ.get("TERMINAL_CWD") or os.getcwd()


def _resolve_session_id() -> str | None:
    """Best-effort owning session id (ACP sets HERMES_SESSION_ID inside _run_agent)."""
    try:
        from gateway.session_context import get_session_env  # type: ignore

        sid = get_session_env("HERMES_SESSION_ID")
        if sid:
            return str(sid)
    except Exception:
        pass
    return os.environ.get("HERMES_SESSION_ID")


class SubagentManager:
    def __init__(
        self,
        *,
        backends: dict[str, SubagentBackend] | None = None,
        cfg: dict[str, Any] | None = None,
        sink_factory: Callable[..., SubagentSink] | None = None,
    ) -> None:
        self._backends: dict[str, SubagentBackend] = dict(backends or {})
        self._cfg = cfg if cfg is not None else config.load_config()
        self._sink_factory = sink_factory or make_sink
        self._records: dict[str, SubagentRecord] = {}
        self._lock = threading.RLock()
        self._fg_lock = threading.Lock()
        self._pending_notifications: list[dict[str, Any]] = []

    # ---- registration -------------------------------------------------------
    def register_backend(self, backend: SubagentBackend) -> None:
        self._backends[backend.name] = backend

    @property
    def config(self) -> dict[str, Any]:
        return self._cfg

    def get(self, agent_id: str) -> SubagentRecord | None:
        with self._lock:
            return self._records.get(agent_id)

    # ---- run_subagent -------------------------------------------------------
    def run(
        self,
        *,
        title: str,
        task: str,
        profile: str,
        is_background: bool = False,
        resume: str | None = None,
        progress_cb: Callable | None = None,
    ) -> dict[str, Any]:
        if resume:
            return self._run_resume(title=title, task=task, resume=resume, progress_cb=progress_cb)

        resolved = config.resolve_profile(profile, self._cfg)  # raises ProfileError
        backend = self._backends.get(resolved.backend)
        if backend is None:
            raise SubagentError(
                f"Backend '{resolved.backend}' (for profile '{profile}') is not registered."
            )
        ok, reason = backend.check_available(resolved)
        if not ok:
            raise SubagentError(f"Backend '{resolved.backend}' unavailable: {reason}")

        if is_background:
            self._enforce_background_cap()

        agent_id = new_agent_id(resolved.backend)
        cwd = _resolve_cwd(self._cfg)
        throttle = float(self._cfg.get("throttle_seconds", 1.0))
        sink = self._sink_factory(
            agent_id, background=is_background, progress_cb=progress_cb, throttle_seconds=throttle
        )
        record = SubagentRecord(
            agent_id=agent_id,
            backend_name=resolved.backend,
            profile_name=profile,
            title=title,
            background=is_background,
            sink=sink,
            session_id=_resolve_session_id(),
        )
        with self._lock:
            self._records[agent_id] = record

        _safe_sink(sink.start, title=title, profile=profile, backend=resolved.backend,
                   task=task, model=resolved.model)

        handle = backend.start(task=task, profile=resolved, cwd=cwd, resume_handle=None)
        record.handle = handle

        if is_background:
            return self._launch_background(record)
        return self._drive_foreground(record)

    def _run_resume(
        self, *, title: str, task: str, resume: str, progress_cb: Callable | None
    ) -> dict[str, Any]:
        prior = self.get(resume)
        if prior is None:
            raise SubagentError(f"Unknown agent_id '{resume}' to resume.")
        resolved = config.resolve_profile(prior.profile_name, self._cfg)
        backend = self._backends.get(resolved.backend)
        if backend is None:
            raise SubagentError(f"Backend '{resolved.backend}' is not registered.")
        ok, reason = backend.check_available(resolved)
        if not ok:
            raise SubagentError(f"Backend '{resolved.backend}' unavailable: {reason}")

        resume_handle = (
            prior.result.backend_session_id if prior.result else None
        ) or getattr(prior.handle, "backend_session_id", None)

        agent_id = new_agent_id(resolved.backend)
        cwd = _resolve_cwd(self._cfg)
        throttle = float(self._cfg.get("throttle_seconds", 1.0))
        sink = self._sink_factory(
            agent_id, background=False, progress_cb=progress_cb, throttle_seconds=throttle
        )
        record = SubagentRecord(
            agent_id=agent_id,
            backend_name=resolved.backend,
            profile_name=prior.profile_name,
            title=title or prior.title,
            background=False,
            sink=sink,
            session_id=_resolve_session_id(),
        )
        with self._lock:
            self._records[agent_id] = record
        _safe_sink(sink.start, title=record.title, profile=record.profile_name,
                   backend=resolved.backend, task=task, model=resolved.model)
        record.handle = backend.start(
            task=task, profile=resolved, cwd=cwd, resume_handle=resume_handle
        )
        return self._drive_foreground(record)

    # ---- foreground ---------------------------------------------------------
    def _drive_foreground(self, record: SubagentRecord) -> dict[str, Any]:
        if not self._fg_lock.acquire(blocking=False):
            # Another foreground subagent is active. Match Devin: one at a time.
            self._fail(record, STATUS_ERROR,
                       error="Another foreground subagent is already running.")
            raise SubagentError("Another foreground subagent is already running.")
        try:
            self._drain(record)
        finally:
            self._fg_lock.release()
        assert record.result is not None
        out = {"agent_id": record.agent_id, "backend": record.backend_name,
               "profile": record.profile_name}
        out.update(record.result.to_dict())
        return out

    # ---- background ---------------------------------------------------------
    def _launch_background(self, record: SubagentRecord) -> dict[str, Any]:
        def _worker() -> None:
            try:
                self._drain(record)
            except Exception as exc:  # never let a worker thread die silently
                logger.warning("background subagent %s crashed: %s", record.agent_id, exc)
                self._fail(record, STATUS_ERROR, error=str(exc))
            finally:
                self._enqueue_wakeup(record)

        t = threading.Thread(target=_worker, name=f"subagent-{record.agent_id}", daemon=True)
        record.thread = t
        t.start()
        return {
            "agent_id": record.agent_id,
            "status": STATUS_RUNNING,
            "backend": record.backend_name,
            "profile": record.profile_name,
            "note": "Running in background. Use read_subagent to collect the result.",
        }

    # ---- shared drain loop --------------------------------------------------
    def _drain(self, record: SubagentRecord) -> None:
        handle = record.handle
        sink = record.sink
        assert handle is not None and sink is not None
        try:
            for ev in handle.events():
                kind = ev.get("kind")
                if kind == "permission":
                    self._handle_permission(record, ev)
                    continue
                text = ev.get("text")
                if text:
                    record.last_activity = text[:200]
                elif ev.get("title"):
                    record.last_activity = str(ev.get("title"))[:200]
                _safe_sink(sink.event, ev)
            result = handle.result()
        except Exception as exc:
            logger.warning("subagent %s drain error: %s", record.agent_id, exc)
            result = SubagentResult(status=STATUS_ERROR, summary=str(exc), error=str(exc))
        self._finish(record, result)

    def _handle_permission(self, record: SubagentRecord, ev: SubagentEvent) -> None:
        perm = ev.get("permission") or {}
        request_id = str(perm.get("request_id") or "")
        handle = record.handle
        sink = record.sink
        # Background subagents run autonomously (no human to prompt); the profile's
        # sandbox is the real guardrail, so we auto-allow and let the backend's
        # sandbox enforce limits. Foreground relays to the sink (editor approval).
        if record.background:
            raw = OUTCOME_ALLOW
        else:
            raw = OUTCOME_DENY
            try:
                if sink is not None:
                    raw = sink.request_permission(perm)
            except Exception as exc:
                logger.warning("permission relay failed for %s: %s", record.agent_id, exc)
                raw = OUTCOME_DENY
        # Normalize to (outcome, concrete option id) so every backend gets a clean
        # signal: codex ignores option_id; devin needs it to select an ACP option.
        outcome, option_id = _normalize_permission(raw, perm.get("options"))
        if handle is not None and request_id:
            try:
                handle.answer_permission(request_id, outcome, option_id)
            except Exception as exc:
                logger.warning("answer_permission failed for %s: %s", record.agent_id, exc)

    # ---- completion bookkeeping --------------------------------------------
    def _finish(self, record: SubagentRecord, result: SubagentResult) -> None:
        record.result = result
        record.status = result.status
        record.finished_at = time.monotonic()
        if result.summary:
            record.last_activity = result.summary[:200]
        _safe_sink(record.sink.done, result) if record.sink else None
        record.completion_event.set()

    def _fail(self, record: SubagentRecord, status: str, *, error: str) -> None:
        result = SubagentResult(status=status, summary=error, error=error, exit_reason=status)
        self._finish(record, result)

    # ---- read_subagent ------------------------------------------------------
    def read(self, agent_id: str, *, block: bool = False, timeout: int = 30) -> dict[str, Any]:
        record = self.get(agent_id)
        if record is None:
            raise SubagentError(f"Unknown agent_id '{agent_id}'.")
        if block and not record.completion_event.is_set():
            timeout = max(0, min(int(timeout or 0), 600))
            record.completion_event.wait(timeout=timeout or None)
        record.notified = True  # reading clears any pending wake-up
        if record.completion_event.is_set() and record.result is not None:
            out = {"agent_id": agent_id, "backend": record.backend_name,
                   "profile": record.profile_name}
            out.update(record.result.to_dict())
            return out
        return {
            "agent_id": agent_id,
            "status": STATUS_RUNNING,
            "backend": record.backend_name,
            "profile": record.profile_name,
            "elapsed_s": record.elapsed_s(),
            "last_activity": record.last_activity,
        }

    # ---- cancel / list ------------------------------------------------------
    def cancel(self, agent_id: str) -> bool:
        record = self.get(agent_id)
        if record is None or record.handle is None:
            return False
        try:
            record.handle.cancel()
        except Exception as exc:
            logger.warning("cancel failed for %s: %s", agent_id, exc)
            return False
        if not record.completion_event.is_set():
            self._fail(record, STATUS_CANCELLED, error="Cancelled.")
        return True

    def cancel_all(self, session_id: str | None = None) -> int:
        n = 0
        with self._lock:
            records = list(self._records.values())
        for r in records:
            if r.completion_event.is_set():
                continue
            if session_id is not None and r.session_id != session_id:
                continue
            if self.cancel(r.agent_id):
                n += 1
        return n

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._records.values())
        return [r.public_state() for r in records]

    # ---- background wake-up -------------------------------------------------
    def _enqueue_wakeup(self, record: SubagentRecord) -> None:
        note = {
            "agent_id": record.agent_id,
            "status": record.status,
            "title": record.title,
            "backend": record.backend_name,
            "profile": record.profile_name,
        }
        with self._lock:
            self._pending_notifications.append(note)

    def drain_notifications(self) -> list[dict[str, Any]]:
        """Pop pending background-completion notifications (for the pre_llm_call hook)."""
        with self._lock:
            notes = self._pending_notifications
            self._pending_notifications = []
        for n in notes:
            rec = self._records.get(n["agent_id"])
            if rec is not None:
                rec.notified = True
        return notes

    def _enforce_background_cap(self) -> None:
        cap = int(self._cfg.get("max_background", 4))
        with self._lock:
            running = sum(
                1 for r in self._records.values()
                if r.background and not r.completion_event.is_set()
            )
        if running >= cap:
            raise SubagentError(
                f"Background subagent limit reached ({cap}). "
                f"Wait for one to finish (read_subagent) before starting another."
            )


_DENY_WORDS = {"deny", "denied", "reject", "rejected", "cancel", "cancelled", "no", "block"}
_ALLOW_WORDS = {"allow", "allowed", "approve", "approved", "yes", "accept", "accepted",
                "once", "session", "always", "selected"}


def _pick_allow_option(options: Any) -> str | None:
    """Choose a concrete allow-like ACP option id from the offered options."""
    opts = options or []
    if not isinstance(opts, list):
        return None
    ids = []
    for o in opts:
        oid = str((o or {}).get("id") or "")
        name = f"{oid} {(o or {}).get('name') or ''}".lower()
        if oid:
            ids.append(oid)
            if any(w in name for w in _ALLOW_WORDS) and not any(w in name for w in _DENY_WORDS):
                return oid
    return ids[0] if ids else None


def _normalize_permission(raw: str, options: Any) -> tuple[str, str | None]:
    """Map a sink/manager outcome (``allow``/``deny`` or a specific option id) to a
    clean ``(outcome, option_id)`` where outcome is :data:`OUTCOME_ALLOW`/``OUTCOME_DENY``."""
    val = str(raw or "").strip()
    low = val.lower()
    if low in _DENY_WORDS or val == OUTCOME_DENY:
        return OUTCOME_DENY, None
    if low in _ALLOW_WORDS or val == OUTCOME_ALLOW:
        return OUTCOME_ALLOW, _pick_allow_option(options)
    # A specific option id was chosen by the editor: deny iff it looks deny-like.
    for o in options or []:
        if str((o or {}).get("id") or "") == val:
            name = f"{val} {(o or {}).get('name') or ''}".lower()
            if any(w in name for w in _DENY_WORDS):
                return OUTCOME_DENY, val
            return OUTCOME_ALLOW, val
    return OUTCOME_ALLOW, val or _pick_allow_option(options)


def _safe_sink(fn: Callable, *args: Any, **kwargs: Any) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # sinks must never break the manager
        logger.debug("sink call failed: %s", exc)


# ---- process-lifetime singleton ---------------------------------------------
_MANAGER: SubagentManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_manager(ctx: Any = None, *, reset: bool = False) -> SubagentManager:
    """Return the process-wide manager, building the default backends on first use."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None or reset:
            _MANAGER = _build_default_manager()
        return _MANAGER


def _build_default_manager() -> SubagentManager:
    cfg = config.load_config()
    mgr = SubagentManager(cfg=cfg)
    # Backends are registered defensively: a backend whose module fails to import
    # (or whose binary is absent) simply isn't available; the others still work.
    for modname, clsname in (
        ("hermes_subagents_overhaul.backends.codex_app_server", "CodexAppServerBackend"),
        ("hermes_subagents_overhaul.backends.devin_acp", "DevinAcpBackend"),
    ):
        try:
            mod = __import__(modname, fromlist=[clsname])
            backend = getattr(mod, clsname)()
            mgr.register_backend(backend)
        except Exception as exc:
            logger.info("subagent backend %s not registered: %s", clsname, exc)
    return mgr
