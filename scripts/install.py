#!/usr/bin/env python3
"""Install hermes-subagents-overhaul into your Hermes environment.

Run with the SAME Python interpreter that has ``hermes-agent`` installed::

    /path/to/hermes-venv/bin/python scripts/install.py            # pip install .
    /path/to/hermes-venv/bin/python scripts/install.py --editable # dev mode

Idempotent. Then enable the toolset in ~/.hermes/config.yaml:

    subagents: { default_backend: codex }
    # CLI/TUI/gateway: ensure the `subagents` toolset is enabled.
    # ACP: acp.enabled_toolsets: [hermes-acp, subagents]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pip_install(editable: bool) -> None:
    target = ["-e", str(REPO_ROOT)] if editable else [str(REPO_ROOT)]
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "--version"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cmd = [sys.executable, "-m", "pip", "install", *target]
    except Exception:
        uv = shutil.which("uv")
        if not uv:
            subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
            cmd = [sys.executable, "-m", "pip", "install", *target]
        else:
            cmd = [uv, "pip", "install", "--python", sys.executable, *target]
    print("[install] " + " ".join(cmd))
    subprocess.check_call(cmd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install hermes-subagents-overhaul.")
    parser.add_argument("--editable", action="store_true", help="pip install -e (dev mode)")
    parser.add_argument("--skip-pip", action="store_true", help="don't pip install")
    args = parser.parse_args(argv)

    if not args.skip_pip:
        _pip_install(editable=args.editable)

    print("\n[install] running doctor...\n")
    try:
        subprocess.call([sys.executable, "-m", "hermes_subagents_overhaul.doctor"])
    except Exception as exc:
        print(f"[install] doctor failed to run: {exc!r}")
    print("\n[install] Done. Enable the `subagents` toolset in ~/.hermes/config.yaml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
