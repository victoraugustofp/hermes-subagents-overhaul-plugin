"""Cross-repo integration: hermes_subagents_overhaul.sinks.acp_sink driving the REAL
hermes_acp_plugin.runtime bridge to a fake ACP connection shaped like the real SDK.

This proves Project A (sinks) and Project B (bridge) interoperate end-to-end:
umbrella + namespaced tool calls reach `conn.session_update`, and a foreground
permission relay round-trips through `conn.request_permission`.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

pytest.importorskip("hermes_acp_plugin.runtime")
acp_schema = pytest.importorskip("acp.schema")

from hermes_acp_plugin import runtime  # noqa: E402

from hermes_subagents_overhaul.backends.base import SubagentResult, event  # noqa: E402
from hermes_subagents_overhaul.sinks.acp_sink import try_make_acp_sink  # noqa: E402


class FakeConn:
    """Mimics the ACP server connection used by HermesACPAgent (async methods)."""

    def __init__(self) -> None:
        self.updates: list = []
        self.perm_requests: list = []

    async def session_update(self, session_id, update):
        self.updates.append((session_id, update))

    async def request_permission(self, session_id, request):
        self.perm_requests.append((session_id, request))
        # Real shape: AllowedOutcome(outcome="selected", optionId=...).
        return acp_schema.RequestPermissionResponse(
            outcome=acp_schema.AllowedOutcome(outcome="selected", optionId="allow")
        )


@pytest.fixture
def acp_session(monkeypatch):
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    conn = FakeConn()
    sid = "sess-int-1"
    runtime.register_session(sid, conn, loop, conn.request_permission)
    monkeypatch.setenv("HERMES_SESSION_ID", sid)
    try:
        yield conn
    finally:
        runtime.unregister_session(sid)
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
        loop.close()


def _dump(update):
    try:
        return update.model_dump(by_alias=True)
    except Exception:
        return {"_repr": repr(update)}


def _kinds(conn):
    return [_dump(u).get("sessionUpdate") for _, u in conn.updates]


def test_background_collapsed_reaches_connection(acp_session):
    conn = acp_session
    sink = try_make_acp_sink("sa_codex_bg", background=True, throttle_seconds=0.0)
    assert sink is not None, "bridge should resolve an active ACP session"
    sink.start(title="job", profile="subagent_general", backend="codex", task="do x", model="gpt")
    sink.event(event("thought", text="thinking"))
    sink.event(event("tool_call", child_id="c1", title="read", tool_kind="read", status="pending"))
    sink.event(event("message", text="progress"))
    sink.done(SubagentResult(status="completed", summary="all done"))
    time.sleep(0.3)

    dumps = [_dump(u) for _, u in conn.updates]
    assert dumps, "no session updates reached the connection"
    # Umbrella start uses the agent_id as toolCallId.
    first = dumps[0]
    assert first.get("sessionUpdate") == "tool_call"
    assert first.get("toolCallId") == "sa_codex_bg"
    # Background must NOT forward child tool-calls as separate ids.
    ids = {d.get("toolCallId") for d in dumps}
    assert "sa_codex_bg:c1" not in ids
    # Terminal update marks completed on the umbrella id.
    last = dumps[-1]
    assert last.get("toolCallId") == "sa_codex_bg"
    assert last.get("status") == "completed"


def test_foreground_rich_forwarding_and_permission(acp_session):
    conn = acp_session
    sink = try_make_acp_sink("sa_codex_fg", background=False)
    assert sink is not None
    sink.start(title="job", profile="coder", backend="codex", task="edit", model="gpt")
    sink.event(event("tool_call", child_id="c1", title="run cmd", tool_kind="execute", status="pending"))
    sink.event(event("tool_update", child_id="c1", status="completed"))
    sink.event(event("diff", child_id="c1", diff={"path": "a.py", "old_text": "x", "new_text": "y"}))
    time.sleep(0.3)

    ids = {_dump(u).get("toolCallId") for _, u in conn.updates}
    assert "sa_codex_fg:c1" in ids, f"expected namespaced child id; saw {ids}"

    # Foreground permission relay round-trips through conn.request_permission.
    outcome = sink.request_permission(
        {"request_id": "p1", "title": "rm -rf", "tool_call": {},
         "options": [{"id": "allow", "name": "Allow"}, {"id": "deny", "name": "Deny"}]}
    )
    assert conn.perm_requests, "request_permission did not reach the connection"
    assert outcome in ("allow", "allow_once") or outcome == "allow"
