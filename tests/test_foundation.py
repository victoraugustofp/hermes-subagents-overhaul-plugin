"""Foundation smoke tests: package imports, contract wiring, profiles, and the
manager's foreground / background / read / cancel paths against a fake backend."""

from __future__ import annotations

import importlib
import json

import pytest

from hermes_subagents_overhaul import config
from hermes_subagents_overhaul.backends.base import STATUS_COMPLETED, STATUS_RUNNING
from hermes_subagents_overhaul.config import ProfileError


def test_package_imports():
    for mod in (
        "hermes_subagents_overhaul",
        "hermes_subagents_overhaul.config",
        "hermes_subagents_overhaul.manager",
        "hermes_subagents_overhaul.tools_schema",
        "hermes_subagents_overhaul.backends.base",
        "hermes_subagents_overhaul.backends.codex_app_server",
        "hermes_subagents_overhaul.backends.devin_acp",
        "hermes_subagents_overhaul.sinks.base",
        "hermes_subagents_overhaul.sinks.acp_sink",
        "hermes_subagents_overhaul.sinks.progress_sink",
        "hermes_subagents_overhaul.contrib",
        "hermes_subagents_overhaul.contrib.tools",
        "hermes_subagents_overhaul.contrib.hooks",
        "hermes_subagents_overhaul.contrib.commands",
        "hermes_subagents_overhaul.doctor",
    ):
        importlib.import_module(mod)


# --- profiles ---------------------------------------------------------------
def test_default_profiles_resolve():
    cfg = config.load_config({})
    names = config.available_profiles(cfg)
    assert {"subagent_explore", "subagent_general", "coder"} <= set(names)
    p = config.resolve_profile("subagent_explore", cfg)
    assert p.backend == "codex" and p.read_only and p.sandbox == "read-only"


def test_unknown_profile_lists_choices():
    cfg = config.load_config({})
    with pytest.raises(ProfileError) as ei:
        config.resolve_profile("nope", cfg)
    assert "Available profiles" in str(ei.value)


def test_user_config_overrides_and_extends():
    cfg = config.load_config({
        "default_backend": "devin",
        "profiles": {
            "subagent_explore": {"model": "gpt-x"},          # override
            "myteam": {"backend": "codex", "model": "m"},     # extend
        },
    })
    assert config.resolve_profile("subagent_explore", cfg).model == "gpt-x"
    assert "myteam" in config.available_profiles(cfg)


# --- contract wiring (register -> tools/hooks/commands) ---------------------
def test_contributors_register_tools_hooks_commands(ctx):
    from hermes_subagents_overhaul import contrib

    ran = contrib.run_contributors(ctx)
    assert {"tools", "hooks", "commands"} <= set(ran)
    assert "run_subagent" in ctx.tools and "read_subagent" in ctx.tools
    assert ctx.tools["run_subagent"]["toolset"] == "subagents"
    assert "pre_llm_call" in ctx.hooks
    assert "subagents" in ctx.commands
    # run_subagent schema exposes the live profile enum.
    schema = ctx.tools["run_subagent"]["schema"]
    assert "subagent_explore" in schema["properties"]["profile"]["enum"]


# --- manager: foreground ----------------------------------------------------
def test_manager_foreground_runs_to_completion(make_manager, recording_sinks):
    mgr = make_manager()
    out = mgr.run(title="t", task="do the thing", profile="subagent_explore")
    assert out["status"] == STATUS_COMPLETED
    assert "completed: do the thing" in out["summary"]
    assert out["backend"] == "codex"
    sink = recording_sinks[0]
    assert sink.started is not None
    assert sink.result is not None and sink.result.status == STATUS_COMPLETED
    # message/thought/tool events were forwarded.
    kinds = [e["kind"] for e in sink.events]
    assert "tool_call" in kinds and "message" in kinds


def test_manager_unknown_profile_raises(make_manager):
    mgr = make_manager()
    with pytest.raises(ProfileError):
        mgr.run(title="t", task="x", profile="does-not-exist")


def test_manager_backend_unavailable(make_manager):
    from tests.conftest import FakeBackend

    mgr = make_manager(backends={"codex": FakeBackend(available=False)})
    from hermes_subagents_overhaul.manager import SubagentError

    with pytest.raises(SubagentError):
        mgr.run(title="t", task="x", profile="subagent_explore")


# --- manager: background + read ---------------------------------------------
def test_manager_background_then_read(make_manager):
    mgr = make_manager()
    out = mgr.run(title="bg", task="async job", profile="subagent_general", is_background=True)
    assert out["status"] == STATUS_RUNNING
    agent_id = out["agent_id"]
    # Blocking read collects the final result.
    res = mgr.read(agent_id, block=True, timeout=10)
    assert res["status"] == STATUS_COMPLETED
    assert "async job" in res["summary"]
    # A completion notification was queued for the wake-up hook.
    notes = mgr.drain_notifications()
    assert any(n["agent_id"] == agent_id for n in notes)


def test_background_cap_enforced(make_manager):
    from hermes_subagents_overhaul.backends.base import SubagentResult
    from hermes_subagents_overhaul.manager import SubagentError

    # A script that never completes until cancelled would be ideal, but we just
    # cap at 0 to assert the guard fires deterministically.
    cfg = config.load_config({"max_background": 0})
    mgr = make_manager(cfg=cfg)
    with pytest.raises(SubagentError):
        mgr.run(title="bg", task="x", profile="subagent_general", is_background=True)


# --- manager: permission relay ----------------------------------------------
def test_foreground_permission_relay(make_manager, recording_sinks):
    from hermes_subagents_overhaul.backends.base import SubagentResult, event
    from tests.conftest import FakeBackend

    def script_factory(task, profile, resume_handle):
        script = [
            event("permission", permission={"request_id": "p1", "title": "rm -rf",
                                             "options": [{"id": "allow", "name": "Allow"}]}),
            event("message", text="after approval"),
        ]
        return script, SubagentResult(status=STATUS_COMPLETED, summary="ok")

    backend = FakeBackend(script_factory=script_factory)
    mgr = make_manager(backends={"codex": backend}, permission_outcome="allow")
    mgr.run(title="t", task="needs approval", profile="subagent_explore")
    sink = recording_sinks[0]
    assert sink.permissions and sink.permissions[0]["request_id"] == "p1"


# --- tool handler returns JSON ----------------------------------------------
def test_run_subagent_handler_returns_json(monkeypatch, ctx):
    from hermes_subagents_overhaul import manager
    from hermes_subagents_overhaul.contrib import tools as tools_contrib
    from tests.conftest import FakeBackend, RecordingSink

    mgr = manager.SubagentManager(
        backends={"codex": FakeBackend()},
        cfg=config.load_config({}),
        sink_factory=lambda agent_id, **kw: RecordingSink(agent_id),
    )
    # contrib.tools binds get_manager into its own namespace, so patch it there.
    monkeypatch.setattr(tools_contrib, "get_manager", lambda *a, **k: mgr)

    tools_contrib.contribute(ctx)
    handler = ctx.tools["run_subagent"]["handler"]
    out = handler({"title": "t", "task": "hello", "profile": "subagent_explore"})
    data = json.loads(out)
    assert data["status"] == STATUS_COMPLETED
