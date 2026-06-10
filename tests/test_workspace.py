"""Unit tests for the subagent workspace resolver (issue #1).

Covers the resolution precedence (explicit workdir > config > ACP per-task cwd >
parent_agent hints > $TERMINAL_CWD > process cwd), path normalisation, and the
loud warnings for a filesystem-root / non-existent workspace.
"""

from __future__ import annotations

import os

import pytest

from hermes_subagents_overhaul import workspace


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    yield


def test_explicit_workdir_wins(tmp_path, monkeypatch):
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    # Explicit workdir beats config, env, everything.
    ws = workspace.resolve({"workspace": str(tmp_path)}, workdir=str(sub))
    assert ws.path == str(sub)
    assert ws.source == "argument"


def test_config_workspace_used_when_concrete(tmp_path):
    ws = workspace.resolve({"workspace": str(tmp_path)})
    assert ws.path == str(tmp_path)
    assert ws.source == "config"


def test_config_auto_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    ws = workspace.resolve({"workspace": "auto"})
    assert ws.path == str(tmp_path)
    assert ws.source == "terminal_cwd_env"


def test_acp_session_cwd_beats_terminal_cwd(tmp_path, monkeypatch):
    """The editor workspace (per-task override) wins over $TERMINAL_CWD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(workspace, "_acp_session_cwd", lambda task_id: str(repo))
    ws = workspace.resolve({"workspace": "auto"}, task_id="sess-1")
    assert ws.path == str(repo)
    assert ws.source == "acp_session"


def test_parent_agent_hint(tmp_path):
    class FakeAgent:
        terminal_cwd = str(tmp_path)

    ws = workspace.resolve({}, parent_agent=FakeAgent())
    assert ws.path == str(tmp_path)
    assert ws.source == "parent_agent"


def test_terminal_cwd_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    ws = workspace.resolve({})
    assert ws.path == str(tmp_path)
    assert ws.source == "terminal_cwd_env"


def test_process_cwd_last_resort(monkeypatch, tmp_path):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.chdir(tmp_path)
    ws = workspace.resolve({})
    assert ws.source == "process_cwd"
    assert os.path.realpath(ws.path) == os.path.realpath(str(tmp_path))


def test_filesystem_root_warns():
    ws = workspace.resolve({"workspace": "/"})
    assert ws.is_root
    assert ws.warning and "filesystem root" in ws.warning
    # Metadata surfaces both the path and the warning.
    meta = ws.to_metadata()
    assert meta["workspace"] == "/"
    assert "workspace_warning" in meta


def test_nonexistent_workspace_warns(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    ws = workspace.resolve({"workspace": missing})
    assert not ws.exists
    assert ws.warning and "does not exist" in ws.warning


def test_git_repo_detected(tmp_path):
    (tmp_path / ".git").mkdir()
    ws = workspace.resolve({"workspace": str(tmp_path)})
    assert ws.is_git_repo
    assert ws.warning is None


def test_sentinel_paths_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    # "." as an explicit workdir is a sentinel and must fall through to config.
    ws = workspace.resolve({"workspace": str(tmp_path)}, workdir=".")
    assert ws.source == "config"
    assert ws.path == str(tmp_path)


def test_acp_session_reads_terminal_tool_overrides(tmp_path):
    """End-to-end: the resolver reads tools.terminal_tool._task_env_overrides."""
    tt = pytest.importorskip("tools.terminal_tool")
    repo = tmp_path / "proj"
    repo.mkdir()
    task_id = "ws-test-task"
    tt.register_task_env_overrides(task_id, {"cwd": str(repo)})
    try:
        ws = workspace.resolve({"workspace": "auto"}, task_id=task_id)
        assert ws.path == str(repo)
        assert ws.source == "acp_session"
    finally:
        tt.clear_task_env_overrides(task_id)
