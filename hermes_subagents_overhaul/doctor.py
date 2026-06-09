"""``hermes-subagents-doctor`` — environment / wiring diagnostics.

Checks: the external backend binaries (``devin``, ``codex``) and their creds, the
optional ACP bridge (``hermes_acp_plugin.runtime``), Hermes importability, and the
resolved profiles. Read-only; exits non-zero if no backend is usable.
"""

from __future__ import annotations

import os
import shutil
import sys

OK = "ok"
WARN = "warn"
FAIL = "fail"


def _line(status: str, label: str, detail: str = "") -> str:
    mark = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[fail]"}.get(status, "[??]  ")
    return f"{mark} {label}" + (f" — {detail}" if detail else "")


def _check_devin() -> tuple[str, str]:
    path = shutil.which("devin")
    if not path:
        return WARN, "devin binary not on PATH (Devin backend disabled)"
    creds = (
        os.environ.get("WINDSURF_API_KEY")
        or (os.environ.get("DEVIN_API_KEY") and os.environ.get("DEVIN_ORG_ID"))
        or os.path.isfile(os.path.expanduser("~/.local/share/devin/credentials.toml"))
        or os.path.isfile(os.path.expanduser("~/.config/devin/config.json"))
    )
    if not creds:
        return WARN, f"{path} found but no Devin creds (WINDSURF_API_KEY / devin auth login)"
    return OK, path


def _check_codex() -> tuple[str, str]:
    path = shutil.which("codex")
    if not path:
        return WARN, "codex binary not on PATH (Codex backend disabled)"
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    creds = os.environ.get("OPENAI_API_KEY") or os.path.isfile(os.path.join(codex_home, "auth.json"))
    if not creds:
        return WARN, f"{path} found but no Codex creds (OPENAI_API_KEY / $CODEX_HOME/auth.json)"
    return OK, path


def _check_bridge() -> tuple[str, str]:
    try:
        import hermes_acp_plugin.runtime  # type: ignore  # noqa: F401

        return OK, "hermes_acp_plugin.runtime present (ACP events enabled)"
    except Exception:
        return WARN, "hermes-acp-plugin bridge not installed (ACP events degrade to progress callback)"


def _check_hermes() -> tuple[str, str]:
    try:
        import toolsets  # type: ignore  # noqa: F401

        return OK, "Hermes importable"
    except Exception as exc:
        return WARN, f"Hermes not importable here ({exc})"


def main() -> int:
    print("hermes-subagents-overhaul doctor\n")
    results: list[tuple[str, str, str]] = []

    devin_s, devin_d = _check_devin()
    codex_s, codex_d = _check_codex()
    bridge_s, bridge_d = _check_bridge()
    hermes_s, hermes_d = _check_hermes()
    results.append((devin_s, "Devin backend (devin acp)", devin_d))
    results.append((codex_s, "Codex backend (codex app-server)", codex_d))
    results.append((bridge_s, "ACP bridge", bridge_d))
    results.append((hermes_s, "Hermes", hermes_d))

    try:
        from hermes_subagents_overhaul import config

        cfg = config.load_config()
        profs = config.available_profiles(cfg)
        results.append((OK, "Profiles", ", ".join(profs) or "(none)"))
    except Exception as exc:
        results.append((FAIL, "Profiles", f"failed to load config: {exc}"))

    for status, label, detail in results:
        print(_line(status, label, detail))

    usable = devin_s == OK or codex_s == OK
    print()
    if usable:
        print("At least one subagent backend is usable.")
        return 0
    print("No usable backend: install/authenticate `devin` or `codex`.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
