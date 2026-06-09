"""Real end-to-end integration tests against the actual `codex` / `devin` binaries.

These exercise the FULL plugin stack (default backends via ``get_manager``) with
trivially cheap tasks. They are gated on the binary + creds being present and on
``HSO_RUN_REAL=1`` so they never fire by accident in CI.

Run them explicitly, e.g.:
    HSO_RUN_REAL=1 .venv/bin/python -m pytest tests/test_integration_real.py -q -s
"""

from __future__ import annotations

import os
import shutil

import pytest

from hermes_subagents_overhaul import config, manager
from hermes_subagents_overhaul.backends.base import STATUS_COMPLETED, STATUS_RUNNING

RUN_REAL = os.environ.get("HSO_RUN_REAL") == "1"

pytestmark = pytest.mark.skipif(not RUN_REAL, reason="set HSO_RUN_REAL=1 to run real e2e")

CHEAP_TASK = "Reply with exactly the word DONE. Do not call any tools."


def _mgr():
    return manager.get_manager(reset=True)


def _backend_ready(mgr: manager.SubagentManager, backend: str, profile: str) -> bool:
    b = mgr._backends.get(backend)
    if b is None:
        return False
    try:
        ok, _ = b.check_available(config.resolve_profile(profile, mgr.config))
    except Exception:
        return False
    return ok


@pytest.mark.skipif(not shutil.which("codex"), reason="codex not installed")
def test_codex_foreground_real():
    mgr = _mgr()
    if not _backend_ready(mgr, "codex", "subagent_explore"):
        pytest.skip("codex backend not available/authed")
    out = mgr.run(title="probe", task=CHEAP_TASK, profile="subagent_explore")
    assert out["status"] in (STATUS_COMPLETED, "failed")
    assert out["status"] == STATUS_COMPLETED, out
    assert out.get("summary")


@pytest.mark.skipif(not shutil.which("codex"), reason="codex not installed")
def test_codex_background_then_read_real():
    mgr = _mgr()
    if not _backend_ready(mgr, "codex", "subagent_general"):
        pytest.skip("codex backend not available/authed")
    started = mgr.run(title="bg", task=CHEAP_TASK, profile="subagent_general", is_background=True)
    assert started["status"] == STATUS_RUNNING
    res = mgr.read(started["agent_id"], block=True, timeout=300)
    assert res["status"] == STATUS_COMPLETED, res
    notes = mgr.drain_notifications()
    assert any(n["agent_id"] == started["agent_id"] for n in notes)


@pytest.mark.skipif(not shutil.which("devin"), reason="devin not installed")
def test_devin_foreground_real():
    mgr = _mgr()
    if not _backend_ready(mgr, "devin", "coder"):
        pytest.skip("devin backend not available/authed")
    out = mgr.run(title="probe", task=CHEAP_TASK, profile="coder")
    assert out["status"] == STATUS_COMPLETED, out
    assert out.get("summary")


@pytest.mark.skipif(not shutil.which("devin"), reason="devin not installed")
def test_devin_background_then_read_real():
    mgr = _mgr()
    if not _backend_ready(mgr, "devin", "coder"):
        pytest.skip("devin backend not available/authed")
    started = mgr.run(title="bg", task=CHEAP_TASK, profile="coder", is_background=True)
    assert started["status"] == STATUS_RUNNING
    res = mgr.read(started["agent_id"], block=True, timeout=300)
    assert res["status"] == STATUS_COMPLETED, res
    assert any(n["agent_id"] == started["agent_id"] for n in mgr.drain_notifications())


@pytest.mark.skipif(not shutil.which("codex"), reason="codex not installed")
def test_codex_resume_real():
    mgr = _mgr()
    if not _backend_ready(mgr, "codex", "subagent_general"):
        pytest.skip("codex backend not available/authed")
    first = mgr.run(title="r1", task="Remember the secret number 42. Reply DONE.",
                    profile="subagent_general")
    assert first["status"] == STATUS_COMPLETED, first
    second = mgr.run(title="r2", task="What was the secret number? Reply with just the number.",
                     profile="subagent_general", resume=first["agent_id"])
    assert second["status"] == STATUS_COMPLETED, second
    assert "42" in (second.get("summary") or "")
