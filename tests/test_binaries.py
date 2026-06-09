"""Robust backend-binary resolution (hermes_subagents_overhaul.binaries).

Guards the fix for the Devin Desktop bug where run_subagent/read_subagent were
hidden because the GUI-launched ACP server had a minimal PATH (no ~/.local/bin,
Homebrew, or nvm) so shutil.which("devin"/"codex") returned None.
"""

from __future__ import annotations

import os
import stat

import pytest

from hermes_subagents_overhaul import binaries


def _make_exec(path):
    path.write_text("#!/bin/sh\necho hi\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def test_resolves_from_explicit_config(tmp_path):
    devin = _make_exec(tmp_path / "devin")
    cfg = {"bin": {"devin": devin}}
    assert binaries.resolve_backend_binary("devin", cfg) == devin


def test_resolves_from_name_bin_config_key(tmp_path):
    codex = _make_exec(tmp_path / "codex")
    cfg = {"codex_bin": codex}
    assert binaries.resolve_backend_binary("codex", cfg) == codex


def test_resolves_from_env_var(tmp_path, monkeypatch):
    devin = _make_exec(tmp_path / "devin")
    monkeypatch.setenv("DEVIN_BIN", devin)
    assert binaries.resolve_backend_binary("devin") == devin


def test_hso_env_var_wins_over_path(tmp_path, monkeypatch):
    devin = _make_exec(tmp_path / "devin")
    monkeypatch.setenv("HSO_DEVIN_BIN", devin)
    assert binaries.resolve_backend_binary("devin") == devin


def test_falls_back_to_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    codex = _make_exec(bindir / "codex")
    monkeypatch.setenv("PATH", str(bindir))
    # no config/env override
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.delenv("HSO_CODEX_BIN", raising=False)
    assert binaries.resolve_backend_binary("codex") == codex


def test_resolves_from_well_known_local_bin_under_minimal_path(tmp_path, monkeypatch):
    """The crux: minimal PATH, but the binary lives in ~/.local/bin."""
    fake_home = tmp_path / "home"
    (fake_home / ".local" / "bin").mkdir(parents=True)
    devin = _make_exec(fake_home / ".local" / "bin" / "devin")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # GUI-like minimal PATH
    monkeypatch.delenv("DEVIN_BIN", raising=False)
    monkeypatch.delenv("HSO_DEVIN_BIN", raising=False)
    assert binaries.resolve_backend_binary("devin") == devin


def test_returns_none_when_absent(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    monkeypatch.delenv("DEVIN_BIN", raising=False)
    monkeypatch.delenv("HSO_DEVIN_BIN", raising=False)
    assert binaries.resolve_backend_binary("devin") is None


def test_child_env_prepends_resolved_dir(tmp_path):
    resolved = str(tmp_path / "tools" / "devin")
    env = binaries.child_env_with_resolved_path(resolved, {"PATH": "/usr/bin:/bin"})
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == str(tmp_path / "tools")
    assert "/usr/bin" in parts  # original preserved


def test_check_fn_visible_under_minimal_path(tmp_path, monkeypatch):
    """The tool check_fn must report available when devin is in ~/.local/bin even
    under a minimal PATH."""
    from hermes_subagents_overhaul.contrib import tools as tools_mod

    fake_home = tmp_path / "home"
    (fake_home / ".local" / "bin").mkdir(parents=True)
    _make_exec(fake_home / ".local" / "bin" / "devin")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("DEVIN_BIN", raising=False)
    monkeypatch.delenv("HSO_DEVIN_BIN", raising=False)
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.delenv("HSO_CODEX_BIN", raising=False)
    assert tools_mod._any_backend_available() is True
