#!/usr/bin/env python3
"""Uninstall hermes-subagents-overhaul from your Hermes environment."""

from __future__ import annotations

import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    cmd = [sys.executable, "-m", "pip", "uninstall", "-y", "hermes-subagents-overhaul"]
    print("[uninstall] " + " ".join(cmd))
    try:
        subprocess.check_call(cmd)
    except Exception as exc:
        print(f"[uninstall] failed: {exc!r}")
        return 1
    print("[uninstall] Done. Remove the `subagents` toolset from ~/.hermes/config.yaml if set.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
