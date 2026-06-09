#!/usr/bin/env python3
"""One-shot setup: install hermes-subagents-overhaul into your real Hermes venv and
enable it across all profiles.

It is idempotent and safe to re-run. By default it:
  1. locates the Python that your `hermes` command actually runs
     (the `hermes` wrapper -> .../venv/bin/python, or ~/.hermes/hermes-agent/venv),
  2. installs THIS repo into that venv (editable), using pip or uv (uv-managed
     venvs that ship no pip are handled),
  3. enables the plugin in every Hermes profile config it finds
     (~/.hermes/config.yaml and ~/.hermes/profiles/*/config.yaml): adds
     `hermes-subagents-overhaul` to `plugins.enabled` and sets
     `acp.enabled_toolsets: [hermes-acp, subagents]` so the tools also appear in
     ACP sessions. Each config is backed up first.
  4. runs `hermes-subagents-doctor`.

Usage (run with ANY python; it finds the right venv itself):
    python scripts/setup.py
    python scripts/setup.py --no-editable           # regular (copied) install
    python scripts/setup.py --skip-install          # only enable in configs
    python scripts/setup.py --skip-enable           # only install into the venv
    python scripts/setup.py --hermes-python /path/to/venv/bin/python
    python scripts/setup.py --config ~/.hermes/profiles/coder/config.yaml  # specific config(s)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = "hermes-subagents-overhaul"
ACP_TOOLSETS = ["hermes-acp", "subagents"]


# --------------------------------------------------------------------------- #
# 1. Locate the Python that `hermes` runs
# --------------------------------------------------------------------------- #
def find_hermes_python(explicit: str | None) -> str:
    if explicit:
        return explicit
    candidates: list[Path] = []
    wrapper = shutil.which("hermes")
    if wrapper:
        try:
            text = Path(wrapper).read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'exec\s+"?([^"\s]+/bin/hermes)"?', text)
            if m:
                candidates.append(Path(m.group(1)).with_name("python"))
        except Exception:
            pass
    candidates.append(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python")
    for c in candidates:
        if c.exists():
            return str(c)
    print(f"[setup] WARNING: could not find the hermes venv python; using {sys.executable}")
    return sys.executable


# --------------------------------------------------------------------------- #
# 2. Install into that venv (pip or uv)
# --------------------------------------------------------------------------- #
def _has_pip(py: str) -> bool:
    return subprocess.run([py, "-m", "pip", "--version"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def install(py: str, editable: bool) -> None:
    spec = ["-e", str(REPO_ROOT)] if editable else [str(REPO_ROOT)]
    if _has_pip(py):
        cmd = [py, "-m", "pip", "install", *spec]
    else:
        uv = shutil.which("uv") or str(Path.home() / ".hermes" / "bin" / "uv")
        if Path(uv).exists() or shutil.which("uv"):
            cmd = [uv, "pip", "install", "--python", py, *spec]
        else:
            print("[setup] no pip and no uv; bootstrapping pip via ensurepip")
            subprocess.check_call([py, "-m", "ensurepip", "--upgrade"])
            cmd = [py, "-m", "pip", "install", *spec]
    print("[setup] " + " ".join(cmd))
    subprocess.check_call(cmd)


# --------------------------------------------------------------------------- #
# 3. Enable in every profile config (comment-preserving)
# --------------------------------------------------------------------------- #
def discover_configs() -> list[Path]:
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    # If HERMES_HOME points at a profile home, its parent .../profiles holds siblings;
    # otherwise treat `home` as the root .hermes.
    root = home
    if root.name and (root.parent.name == "profiles"):
        root = root.parent.parent
    found: list[Path] = []
    default_cfg = root / "config.yaml"
    if default_cfg.is_file():
        found.append(default_cfg)
    for p in sorted(glob.glob(str(root / "profiles" / "*" / "config.yaml"))):
        found.append(Path(p))
    return found


def _backup(path: Path) -> None:
    ts = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
    bak = path.with_name(path.name + f".bak-hso-{ts}")
    shutil.copy2(path, bak)
    print(f"[setup]   backed up {path} -> {bak.name}")


def enable_in_config(path: Path) -> bool:
    """Add the plugin to plugins.enabled and set acp.enabled_toolsets. Returns
    True if the file changed. Uses ruamel.yaml to preserve comments/formatting."""
    try:
        from ruamel.yaml import YAML  # bundled with hermes-agent
    except Exception:
        print("[setup]   ruamel.yaml unavailable; skipping config edit for", path)
        return False
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.load(fh) or {}

    changed = False
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        data["plugins"] = plugins
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        enabled = []
        plugins["enabled"] = enabled
    if PLUGIN_NAME not in enabled:
        enabled.append(PLUGIN_NAME)
        changed = True
    disabled = plugins.get("disabled")
    if isinstance(disabled, list) and PLUGIN_NAME in disabled:
        disabled.remove(PLUGIN_NAME)
        changed = True

    acp = data.get("acp")
    if not isinstance(acp, dict):
        acp = {}
        data["acp"] = acp
    cur = acp.get("enabled_toolsets")
    # Normalize a stray string (e.g. a bad `config set`) and merge in our toolsets.
    cur_list = cur if isinstance(cur, list) else ([] if cur in (None, "") else [cur])
    merged = list(dict.fromkeys([*cur_list, *ACP_TOOLSETS]))
    # Drop any obviously-bogus single-string-list entry like "[hermes-acp, subagents]".
    merged = [t for t in merged if not (isinstance(t, str) and t.strip().startswith("["))]
    merged = list(dict.fromkeys([*merged, *ACP_TOOLSETS]))
    if merged != cur:
        acp["enabled_toolsets"] = merged
        changed = True

    if changed:
        _backup(path)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
    return changed


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install + enable hermes-subagents-overhaul.")
    ap.add_argument("--hermes-python", help="Path to the python of your hermes venv.")
    ap.add_argument("--no-editable", action="store_true", help="regular (copied) install")
    ap.add_argument("--skip-install", action="store_true", help="only enable in configs")
    ap.add_argument("--skip-enable", action="store_true", help="only install into the venv")
    ap.add_argument("--config", action="append", default=[],
                    help="specific config.yaml to enable in (repeatable). "
                         "Default: all discovered profiles.")
    args = ap.parse_args(argv)

    py = find_hermes_python(args.hermes_python)
    print(f"[setup] hermes venv python: {py}")

    if not args.skip_install:
        install(py, editable=not args.no_editable)

    if not args.skip_enable:
        configs = [Path(os.path.expanduser(c)) for c in args.config] or discover_configs()
        if not configs:
            print("[setup] no Hermes config.yaml found to enable in.")
        for cfg in configs:
            if not cfg.is_file():
                print(f"[setup] skip (not found): {cfg}")
                continue
            changed = enable_in_config(cfg)
            print(f"[setup] {'updated' if changed else 'already enabled'}: {cfg}")

    print("\n[setup] running doctor...\n")
    subprocess.call([py, "-m", "hermes_subagents_overhaul.doctor"])
    print("\n[setup] Done. Restart any running `hermes` sessions to pick up the plugin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
