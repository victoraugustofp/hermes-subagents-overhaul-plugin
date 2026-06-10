"""Workspace (cwd) resolution for subagents — the single source of truth for
*which directory* a subagent runs in.

The bug this fixes (issue #1): subagents launched from a Hermes/ACP session
silently fell back to the ACP server's process cwd (often ``/`` under a
GUI-launched server) because the editor-provided workspace root was never
forwarded. Different profiles/backends then disagreed on the workspace, and a
"investigate the repo" prompt could end up inspecting the macOS filesystem root.

Resolution precedence (first concrete, non-sentinel hit wins):

1. explicit per-call ``workdir`` argument to ``run_subagent`` (caller override);
2. configured ``subagents.workspace`` when set to a concrete path (not ``auto``);
3. the ACP session's editor workspace, looked up by ``task_id`` in
   ``tools.terminal_tool._task_env_overrides`` — this is exactly the ``cwd`` the
   ACP adapter registers from ``session/new`` (the editor's project root). In the
   ACP adapter ``task_id == session_id``, and the plugin tool layer hands the
   tool handler that ``task_id``;
4. parent-agent hints (``terminal_cwd`` / ``cwd`` / ``_subdirectory_hints``) — the
   CLI / ``dispatch_tool`` path, mirroring the built-in ``delegate_tool``;
5. ``$TERMINAL_CWD`` (the editor/terminal anchor when exported into the env);
6. the process cwd (``os.getcwd()``) as a last resort.

The chosen path is expanded/normalised and annotated so callers can surface a
trustworthy ``workspace`` field — and a loud, actionable warning when it resolves
to a filesystem root or a path that does not exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# cwd values that are not real anchors (mirror file_tools.py / the acp tool).
_SENTINELS = {"", ".", "./", "auto", "cwd"}


@dataclass(frozen=True)
class Workspace:
    """A resolved subagent workspace plus provenance and validity flags."""

    path: str
    source: str            # which candidate won (argument|config|acp_session|...)
    exists: bool
    is_dir: bool
    is_root: bool          # filesystem root ("/" or a bare drive root)
    is_git_repo: bool
    warning: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        """Compact, trustworthy runner-level metadata for tool responses."""
        d: dict[str, Any] = {
            "workspace": self.path,
            "workspace_source": self.source,
            "is_git_repo": self.is_git_repo,
        }
        if self.warning:
            d["workspace_warning"] = self.warning
        return d


def _clean(raw: Any) -> str | None:
    """Normalise a candidate path, or ``None`` if it is empty/sentinel/non-str."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.lower() in _SENTINELS:
        return None
    try:
        return os.path.normpath(os.path.expanduser(s))
    except Exception:
        return None


def _acp_session_cwd(task_id: str | None) -> str | None:
    """The editor workspace the ACP adapter registered for this session/task.

    ``acp_adapter`` binds the ``session/new`` ``cwd`` to the session id via
    ``tools.terminal_tool.register_task_env_overrides(session_id, {"cwd": ...})``.
    The model-invoked tool layer passes that same id to handlers as ``task_id``.
    Read it back defensively — the table is process-global and may be absent
    (CLI / tests / minimal installs).
    """
    if not task_id:
        return None
    try:
        from tools.terminal_tool import _task_env_overrides  # type: ignore

        overrides = _task_env_overrides.get(task_id) or {}
        return overrides.get("cwd")
    except Exception:
        return None


def _parent_agent_cwd(parent_agent: Any) -> str | None:
    """Best-effort workspace from the parent agent (CLI / ``dispatch_tool`` path).

    Mirrors the built-in ``delegate_tool._resolve_workspace_hint`` candidate set
    so subagents inherit the same workspace the parent agent would use.
    """
    if parent_agent is None:
        return None
    hints = (
        getattr(getattr(parent_agent, "_subdirectory_hints", None), "working_dir", None),
        getattr(parent_agent, "terminal_cwd", None),
        getattr(parent_agent, "cwd", None),
    )
    for hint in hints:
        if isinstance(hint, str) and hint.strip():
            return hint
    return None


def _safe_getcwd() -> str:
    try:
        return os.getcwd()
    except Exception:
        return os.environ.get("TERMINAL_CWD") or os.path.expanduser("~")


def _is_fs_root(path: str) -> bool:
    """True for POSIX ``/`` or a bare Windows drive root (``C:\\`` / ``C:/``)."""
    if path in ("/", os.sep):
        return True
    drive, tail = os.path.splitdrive(path)
    return bool(drive) and tail in ("", os.sep, "/", "\\")


def _looks_like_git_repo(path: str) -> bool:
    """Walk up from ``path`` looking for a ``.git`` dir/file (worktree-aware)."""
    try:
        cur = os.path.abspath(path)
    except Exception:
        return False
    while True:
        dot_git = os.path.join(cur, ".git")
        if os.path.isdir(dot_git) or os.path.isfile(dot_git):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def _annotate(path: str, source: str) -> Workspace:
    try:
        exists = os.path.exists(path)
        is_dir = os.path.isdir(path)
    except Exception:
        exists = is_dir = False
    is_root = _is_fs_root(path)
    is_git = _looks_like_git_repo(path) if is_dir else False

    warning: str | None = None
    if is_root:
        warning = (
            f"Subagent workspace resolved to the filesystem root ({path}) via "
            f"'{source}'; no project repository is in scope. Pass an explicit "
            f"'workdir', set subagents.workspace, or open a project in your editor "
            f"so the subagent operates on the intended repo instead of the whole "
            f"filesystem."
        )
    elif not exists or not is_dir:
        warning = (
            f"Subagent workspace '{path}' (via '{source}') does not exist or is not "
            f"a directory; the subagent may fail to start or inspect the wrong "
            f"location. Pass an explicit 'workdir' that points at the intended repo."
        )
    return Workspace(
        path=path,
        source=source,
        exists=exists,
        is_dir=is_dir,
        is_root=is_root,
        is_git_repo=is_git,
        warning=warning,
    )


def resolve(
    cfg: dict[str, Any] | None = None,
    *,
    task_id: str | None = None,
    parent_agent: Any = None,
    workdir: str | None = None,
) -> Workspace:
    """Resolve the effective subagent workspace (see module docstring for order)."""
    cfg = cfg or {}

    config_ws = cfg.get("workspace")
    if isinstance(config_ws, str) and config_ws.strip().lower() == "auto":
        config_ws = None

    candidates: list[tuple[str, Any]] = [
        ("argument", workdir),
        ("config", config_ws),
        ("acp_session", _acp_session_cwd(task_id)),
        ("parent_agent", _parent_agent_cwd(parent_agent)),
        ("terminal_cwd_env", os.environ.get("TERMINAL_CWD")),
    ]

    for source, raw in candidates:
        cleaned = _clean(raw)
        if cleaned:
            return _annotate(cleaned, source)

    return _annotate(_safe_getcwd(), "process_cwd")
