"""Regression tests for issue #1: consistent workspace propagation.

Every profile/backend launched from the same parent context must receive the
SAME effective workspace, the effective workspace must be visible in runner
metadata (run + read + list), and a "/"-style workspace must surface a loud
warning instead of being silently inspected.
"""

from __future__ import annotations

from typing import Any

import pytest

from hermes_subagents_overhaul import config
from hermes_subagents_overhaul.backends.base import STATUS_COMPLETED, STATUS_RUNNING
from hermes_subagents_overhaul.manager import SubagentManager
from hermes_subagents_overhaul.sinks.base import SubagentSink
from tests.conftest import FakeBackend, RecordingSink

# The five built-in profiles exercised in the issue, spanning both backends.
ALL_PROFILES = [
    "coder",                # devin
    "debugger",             # codex
    "frontend-developer",   # devin
    "subagent_explore",     # codex
    "subagent_general",     # codex
]


def _make_manager(cfg: dict[str, Any]) -> tuple[SubagentManager, dict[str, FakeBackend]]:
    backends = {"codex": FakeBackend("codex"), "devin": FakeBackend("devin")}

    def sink_factory(agent_id: str, *, background: bool = False, **kw: Any) -> SubagentSink:
        return RecordingSink(agent_id, background=background)

    mgr = SubagentManager(backends=backends, cfg=cfg, sink_factory=sink_factory)
    return mgr, backends


def _started_cwd(backends: dict[str, FakeBackend], backend_name: str) -> str:
    started = backends[backend_name].started
    assert started, f"{backend_name} backend was never started"
    return started[-1]["cwd"]


def test_all_profiles_get_same_workspace(tmp_path):
    """coder/debugger/frontend-developer/subagent_explore/subagent_general all
    receive the identical effective workspace handed by the runner."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = config.load_config({"workspace": str(repo)})
    mgr, backends = _make_manager(cfg)

    seen: dict[str, str] = {}
    for profile in ALL_PROFILES:
        out = mgr.run(title=f"smoke {profile}", task="identify workspace", profile=profile)
        assert out["status"] == STATUS_COMPLETED, profile
        # Runner-level workspace metadata is present and correct.
        assert out["workspace"] == str(repo), profile
        assert out["workspace_source"] == "config", profile
        seen[profile] = out["workspace"]

    # Every profile agreed on the workspace.
    assert set(seen.values()) == {str(repo)}
    # And every backend actually received that cwd (not "/").
    assert _started_cwd(backends, "codex") == str(repo)
    assert _started_cwd(backends, "devin") == str(repo)


def test_acp_session_workspace_propagates_to_every_backend(tmp_path):
    """The exact reported bug: with a real ACP per-task cwd override, codex- and
    devin-backed profiles alike must use it (not the process cwd "/")."""
    tt = pytest.importorskip("tools.terminal_tool")
    repo = tmp_path / "agent-runtime"
    repo.mkdir()
    task_id = "acp-sess-prop"
    tt.register_task_env_overrides(task_id, {"cwd": str(repo)})
    try:
        cfg = config.load_config({"workspace": "auto"})
        mgr, backends = _make_manager(cfg)
        for profile in ALL_PROFILES:
            out = mgr.run(title="t", task="x", profile=profile, task_id=task_id)
            assert out["workspace"] == str(repo), profile
            assert out["workspace_source"] == "acp_session", profile
        assert _started_cwd(backends, "codex") == str(repo)
        assert _started_cwd(backends, "devin") == str(repo)
    finally:
        tt.clear_task_env_overrides(task_id)


def test_explicit_workdir_overrides(tmp_path):
    repo = tmp_path / "repo"
    sub = repo / "frontend"
    sub.mkdir(parents=True)
    cfg = config.load_config({"workspace": str(repo)})
    mgr, backends = _make_manager(cfg)
    out = mgr.run(title="t", task="x", profile="frontend-developer", workdir=str(sub))
    assert out["workspace"] == str(sub)
    assert out["workspace_source"] == "argument"
    assert _started_cwd(backends, "devin") == str(sub)


def test_foreground_and_background_agree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = config.load_config({"workspace": str(repo)})
    mgr, _ = _make_manager(cfg)

    fg = mgr.run(title="fg", task="x", profile="subagent_general")
    bg = mgr.run(title="bg", task="x", profile="subagent_general", is_background=True)
    assert bg["status"] == STATUS_RUNNING
    assert fg["workspace"] == repo.__str__() == bg["workspace"]

    # read_subagent surfaces the same workspace for the background run.
    res = mgr.read(bg["agent_id"], block=True, timeout=10)
    assert res["status"] == STATUS_COMPLETED
    assert res["workspace"] == str(repo)

    # list() also carries the workspace for both.
    states = {s["agent_id"]: s for s in mgr.list()}
    assert states[bg["agent_id"]]["workspace"] == str(repo)


def test_handler_threads_task_id_and_workdir(tmp_path, monkeypatch):
    """The run_subagent tool handler forwards task_id + workdir to the manager,
    and the returned JSON carries the effective workspace."""
    import json

    from hermes_subagents_overhaul.contrib import tools as tools_contrib

    repo = tmp_path / "repo"
    repo.mkdir()
    mgr, backends = _make_manager(config.load_config({"workspace": "auto"}))
    monkeypatch.setattr(tools_contrib, "get_manager", lambda *a, **k: mgr)

    # workdir arg overrides everything; task_id would otherwise be consulted.
    out = tools_contrib._run_subagent_handler(
        {"title": "t", "task": "x", "profile": "coder", "workdir": str(repo)},
        task_id="some-session",
    )
    data = json.loads(out)
    assert data["workspace"] == str(repo)
    assert data["workspace_source"] == "argument"
    assert _started_cwd(backends, "devin") == str(repo)


def test_root_workspace_warns_consistently(tmp_path):
    """When the workspace is "/", all profiles get "/" AND a loud warning."""
    cfg = config.load_config({"workspace": "/"})
    mgr, backends = _make_manager(cfg)
    for profile in ALL_PROFILES:
        out = mgr.run(title="t", task="x", profile=profile)
        assert out["workspace"] == "/", profile
        assert "workspace_warning" in out, profile
        assert "filesystem root" in out["workspace_warning"], profile
    # Consistent "/" across backends (the issue's "pass / consistently" branch).
    assert _started_cwd(backends, "codex") == "/"
    assert _started_cwd(backends, "devin") == "/"


def test_resume_without_explicit_workdir_uses_resumed_source(tmp_path):
    """When resuming without an explicit workdir, the source should be 'resumed'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = config.load_config({"workspace": str(repo)})
    mgr, _ = _make_manager(cfg)

    # Run the initial task.
    first = mgr.run(title="first", task="x", profile="coder")
    assert first["workspace"] == str(repo)
    assert first["workspace_source"] == "config"

    # Resume without explicit workdir; should inherit workspace with "resumed" source.
    second = mgr.run(title="second", task="y", profile="coder", resume=first["agent_id"])
    assert second["workspace"] == str(repo)
    assert second["workspace_source"] == "resumed", "Resume without explicit workdir should report 'resumed' source"


def test_resume_with_explicit_workdir_uses_argument_source(tmp_path):
    """When resuming with an explicit workdir, the source should be 'argument'."""
    repo = tmp_path / "repo"
    sub = repo / "sub"
    repo.mkdir()
    sub.mkdir()
    cfg = config.load_config({"workspace": str(repo)})
    mgr, _ = _make_manager(cfg)

    # Run the initial task.
    first = mgr.run(title="first", task="x", profile="coder")
    assert first["workspace"] == str(repo)

    # Resume with explicit workdir override; should use that with "argument" source.
    second = mgr.run(title="second", task="y", profile="coder", resume=first["agent_id"], workdir=str(sub))
    assert second["workspace"] == str(sub)
    assert second["workspace_source"] == "argument", "Resume with explicit workdir should report 'argument' source"


def test_is_git_repo_in_metadata(tmp_path):
    """The is_git_repo field should be present in run/read/list responses."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # Make it a git repo.
    cfg = config.load_config({"workspace": str(repo)})
    mgr, _ = _make_manager(cfg)

    # run_subagent response includes is_git_repo.
    out = mgr.run(title="t", task="x", profile="coder")
    assert "is_git_repo" in out
    assert out["is_git_repo"] is True

    # read_subagent response includes is_git_repo.
    res = mgr.read(out["agent_id"])
    assert "is_git_repo" in res
    assert res["is_git_repo"] is True

    # list() response includes is_git_repo.
    states = mgr.list()
    assert len(states) > 0
    assert "is_git_repo" in states[0]
    assert states[0]["is_git_repo"] is True


def test_is_git_repo_false_for_non_repo(tmp_path):
    """The is_git_repo field should be False for non-git directories."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = config.load_config({"workspace": str(repo)})
    mgr, _ = _make_manager(cfg)

    out = mgr.run(title="t", task="x", profile="coder")
    assert "is_git_repo" in out
    assert out["is_git_repo"] is False
