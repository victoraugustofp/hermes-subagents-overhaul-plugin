"""Robust resolution of backend CLI binaries (``devin`` / ``codex``).

WHY THIS EXISTS
---------------
When Hermes runs as an ACP server launched by a **GUI** client (Devin Desktop /
Windsurf), the subprocess inherits the GUI's minimal ``PATH`` (typically just
``/usr/bin:/bin:/usr/sbin:/sbin``) — **not** the login-shell ``PATH``. So
``shutil.which("devin")`` / ``shutil.which("codex")`` return ``None`` even though
the binaries are installed under ``~/.local/bin``, Homebrew, or an ``nvm`` node
dir. That made the ``run_subagent`` / ``read_subagent`` tools' ``check_fn`` fail,
so Hermes **filtered the tools out** (the model only saw built-in ``delegate_task``),
and any spawn via ``Popen(["devin", ...])`` would also ``FileNotFoundError``.

This module resolves a backend binary to an **absolute path** by checking, in
order: plugin config → environment variables → ``PATH`` → well-known install
locations. Used by both the tool ``check_fn`` (visibility) and the backends
(spawn), so behavior is consistent regardless of the inherited ``PATH``.
"""

from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path
from typing import Any, Optional

# Per-backend environment variable overrides (first match wins).
_ENV_VARS = {
    "devin": ("HSO_DEVIN_BIN", "DEVIN_BIN"),
    "codex": ("HSO_CODEX_BIN", "CODEX_BIN"),
}


def _is_exec(path: Optional[str]) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _common_locations(name: str) -> list[str]:
    """Well-known install locations for *name*, most-specific first."""
    home = _home()
    locs: list[Path] = [
        home / ".local" / "bin" / name,
        Path("/opt/homebrew/bin") / name,  # Apple-silicon Homebrew
        Path("/usr/local/bin") / name,     # Intel Homebrew / manual installs
        Path("/usr/bin") / name,
    ]
    if name == "devin":
        # Devin CLI's versioned install layout.
        locs.append(
            home / ".local" / "share" / "devin" / "cli" / "_versions" / "current" / "bin" / "devin"
        )
    # node-based installs (codex is commonly an npm global under nvm/fnm/volta).
    node_globs = [
        str(home / ".nvm" / "versions" / "node" / "*" / "bin" / name),
        str(home / ".fnm" / "node-versions" / "*" / "installation" / "bin" / name),
        str(home / "Library" / "pnpm" / name),
    ]
    for pattern in node_globs:
        for match in sorted(glob.glob(pattern), reverse=True):  # newest first
            locs.append(Path(match))
    locs.append(home / ".npm-global" / "bin" / name)
    locs.append(home / ".volta" / "bin" / name)
    return [str(p) for p in locs]


def resolve_backend_binary(name: str, cfg: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Return an absolute path to the *name* CLI, or ``None`` if not found.

    Resolution order: config (``subagents.bin.<name>`` or ``subagents.<name>_bin``)
    → env (``HSO_<NAME>_BIN`` / ``<NAME>_BIN``) → ``PATH`` → well-known locations.
    """
    # 1. Plugin config (explicit path wins).
    if cfg:
        explicit = None
        bin_map = cfg.get("bin")
        if isinstance(bin_map, dict):
            explicit = bin_map.get(name)
        explicit = explicit or cfg.get(f"{name}_bin")
        if explicit:
            cand = os.path.expanduser(str(explicit))
            if _is_exec(cand):
                return cand

    # 2. Environment variable overrides.
    for env_var in _ENV_VARS.get(name, ()):
        val = os.environ.get(env_var)
        if val:
            cand = os.path.expanduser(val)
            if _is_exec(cand):
                return cand

    # 3. PATH (works when launched from a login shell / after PATH augmentation).
    found = shutil.which(name)
    if found:
        return found

    # 4. Well-known install locations (handles the minimal GUI PATH case).
    for cand in _common_locations(name):
        if _is_exec(cand):
            return cand
    return None


def child_env_with_resolved_path(
    resolved_bin: Optional[str], base_env: Optional[dict[str, str]] = None
) -> dict[str, str]:
    """Return a copy of *base_env* (default ``os.environ``) whose ``PATH`` includes
    the resolved binary's directory plus common bin dirs — so the spawned child
    (e.g. ``devin acp`` / ``codex``) can find its own dependencies (node, etc.)
    even under a minimal inherited PATH."""
    env = dict(base_env if base_env is not None else os.environ)
    extra: list[str] = []
    if resolved_bin:
        extra.append(os.path.dirname(resolved_bin))
    home = _home()
    extra.extend(
        [
            str(home / ".local" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
        ]
    )
    # newest node bin (for npm-installed CLIs that re-exec node)
    for pattern in (str(home / ".nvm" / "versions" / "node" / "*" / "bin"),):
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            extra.append(matches[0])
    existing = env.get("PATH", "")
    parts = [p for p in extra if p]  # prepend our dirs
    if existing:
        parts.append(existing)
    # de-dup while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for p in parts:
        for seg in p.split(os.pathsep):
            if seg and seg not in seen:
                seen.add(seg)
                ordered.append(seg)
    env["PATH"] = os.pathsep.join(ordered)
    return env
