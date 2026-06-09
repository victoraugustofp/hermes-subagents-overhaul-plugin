"""Codex backend — drives ``codex app-server`` (native JSON-RPC over stdio).

STUB / CONTRACT ONLY. The full implementation is the WS1 workstream and OWNS this
file end-to-end. It must:

* Reuse Hermes' existing client:
  ``from agent.transports.codex_app_server_session import CodexAppServerSession``
  (and ``CodexAppServerClient`` / ``CodexEventProjector``). These are standalone
  (no AIAgent coupling) — see docs/research/codex-app-server.md.
* ``start(...)`` spawns a worker thread that calls ``session.run_turn(task, ...)``
  with an ``on_event`` callback that translates each codex notification
  (``item/started``, ``item/completed``, ``turn/completed``,
  ``thread/tokenUsage/updated``, ...) into a normalized
  :data:`~hermes_subagents_overhaul.backends.base.SubagentEvent` pushed onto a
  queue that :meth:`SubagentHandle.events` yields from.
* Map ``ResolvedProfile`` -> ``extra_args=['-c', f'model="{model}"', '-c',
  f'sandbox_mode="{sandbox}"']`` (or ``permission_profile``); read-only profiles
  use ``sandbox_mode="read-only"``.
* Bridge ``approval_callback(command_label, description, allow_permanent)`` <->
  the permission rendezvous (yield a ``kind="permission"`` event; block until
  :meth:`answer_permission`).
* ``backend_session_id`` = the codex ``thread_id`` (for resume via
  ``codex exec resume <id>`` or app-server thread resume).
"""

from __future__ import annotations

from hermes_subagents_overhaul.backends.base import SubagentHandle
from hermes_subagents_overhaul.config import ResolvedProfile


class CodexAppServerBackend:
    name = "codex"

    def check_available(self, profile: ResolvedProfile) -> tuple[bool, str]:
        return (False, "CodexAppServerBackend not yet implemented (WS1).")

    def start(
        self,
        *,
        task: str,
        profile: ResolvedProfile,
        cwd: str,
        resume_handle: str | None = None,
    ) -> SubagentHandle:
        raise NotImplementedError("CodexAppServerBackend.start (WS1)")
