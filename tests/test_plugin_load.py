"""Real plugin-load test: load this plugin through Hermes' actual PluginManager
(entry-point discovery) and confirm the tools + hooks register.

Entry-point plugins are opt-in via ``plugins.enabled`` in ~/.hermes/config.yaml, so
we point HERMES_HOME at a temp dir whose config enables us. Skips if Hermes isn't
importable in this environment.
"""

from __future__ import annotations

import os
import textwrap

import pytest

pytest.importorskip("hermes_cli.plugins")


@pytest.fixture
def hermes_home_with_plugin_enabled(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        textwrap.dedent(
            """
            plugins:
              enabled:
                - hermes-subagents-overhaul
              disabled: []
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    yield home


def test_plugin_loads_via_entry_point(hermes_home_with_plugin_enabled):
    from hermes_cli import plugins as P

    mgr = P.PluginManager()
    mgr.discover_and_load(force=True)

    tool_names = set(getattr(mgr, "_plugin_tool_names", set()))
    assert "run_subagent" in tool_names, f"tools registered: {sorted(tool_names)}"
    assert "read_subagent" in tool_names

    hooks = getattr(mgr, "_hooks", {})
    assert "pre_llm_call" in hooks
    assert "on_session_end" in hooks

    # The plugin record should be marked enabled/loaded without error.
    plugins = getattr(mgr, "_plugins", {})
    rec = plugins.get("hermes-subagents-overhaul")
    assert rec is not None
    assert getattr(rec, "error", None) in (None, ""), getattr(rec, "error", None)
