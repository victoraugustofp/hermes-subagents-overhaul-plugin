"""Devin backend — spawns ``devin acp`` (native ACP server over stdio) and drives
it as a PURE ACP CLIENT.

STUB / CONTRACT ONLY. The full implementation is the WS3 workstream and OWNS this
file end-to-end. It must:

* Spawn ``devin [--model M] [--sandbox] [--permission-mode MODE] acp`` (global flags
  precede the ``acp`` subcommand; or set ``DEVIN_MODEL`` / ``DEVIN_SANDBOX`` /
  ``DEVIN_PERMISSION_MODE`` in the child env). See docs/research/devin-acp-client.md.
* Act as an ACP client over stdio: ``initialize`` -> ``session/new`` (cwd) ->
  ``session/prompt`` (task); consume ``session/update`` notifications
  (``agent_message_chunk``, ``agent_thought_chunk``, ``tool_call``,
  ``tool_call_update``, ``plan``) until the prompt response's stop reason; honor
  inbound ``session/request_permission``. Hermes' ``agent/copilot_acp_client.py``
  only surfaces text/thought and auto-denies permissions, so vendor a fuller sync
  JSON-RPC client (reuse its stdio framing) OR drive the installed ``acp`` SDK
  client — translate each update into a normalized SubagentEvent on a queue that
  :meth:`SubagentHandle.events` yields from.
* Map read-only profiles to ``--permission-mode auto`` (+ no Write scopes) per V-2.
* ``backend_session_id`` = the child ACP ``sessionId`` (for resume via
  ``session/load`` or ``devin -r``).
"""

from __future__ import annotations

from hermes_subagents_overhaul.backends.base import SubagentHandle
from hermes_subagents_overhaul.config import ResolvedProfile


class DevinAcpBackend:
    name = "devin"

    def check_available(self, profile: ResolvedProfile) -> tuple[bool, str]:
        return (False, "DevinAcpBackend not yet implemented (WS3).")

    def start(
        self,
        *,
        task: str,
        profile: ResolvedProfile,
        cwd: str,
        resume_handle: str | None = None,
    ) -> SubagentHandle:
        raise NotImplementedError("DevinAcpBackend.start (WS3)")
