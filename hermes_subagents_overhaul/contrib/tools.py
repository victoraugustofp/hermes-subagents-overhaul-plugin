"""Registers the ``run_subagent`` / ``read_subagent`` tools into the ``subagents``
toolset (also merged into the ``hermes-acp`` toolset surface where applicable)."""

from __future__ import annotations

import json
import logging
from typing import Any

from hermes_subagents_overhaul import binaries, config, tools_schema
from hermes_subagents_overhaul.config import ProfileError
from hermes_subagents_overhaul.manager import SubagentError, get_manager

logger = logging.getLogger("hermes_subagents_overhaul.contrib.tools")

TOOLSET = "subagents"


def _progress_cb_from_kwargs(kwargs: dict[str, Any]):
    parent = kwargs.get("parent_agent")
    cb = getattr(parent, "tool_progress_callback", None)
    return cb if callable(cb) else None


def _run_subagent_handler(args: dict[str, Any], **kwargs: Any) -> str:
    mgr = get_manager()
    title = str(args.get("title") or "").strip()
    task = str(args.get("task") or "").strip()
    profile = str(args.get("profile") or "").strip()
    is_background = bool(args.get("is_background", False))
    resume = args.get("resume") or None
    workdir = str(args.get("workdir") or "").strip() or None
    # ``task_id`` is threaded into every model-invoked tool by the host; under
    # the ACP adapter it IS the session id, which is the key the editor's
    # workspace cwd was registered under. ``parent_agent`` is only present on the
    # CLI / ``dispatch_tool`` path. Both feed the workspace resolver so subagents
    # consistently inherit the active project root instead of falling back to "/".
    task_id = kwargs.get("task_id") or None
    parent_agent = kwargs.get("parent_agent")
    if not task:
        return "Error: 'task' is required."
    if not resume and not profile:
        return "Error: 'profile' is required (or pass 'resume')."
    try:
        out = mgr.run(
            title=title or "subagent",
            task=task,
            profile=profile,
            is_background=is_background,
            resume=resume,
            progress_cb=_progress_cb_from_kwargs(kwargs),
            task_id=task_id,
            parent_agent=parent_agent,
            workdir=workdir,
        )
    except (ProfileError, SubagentError) as exc:
        return f"Error: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("run_subagent failed: %s", exc)
        return f"Error: run_subagent failed: {exc}"
    return json.dumps(out, ensure_ascii=False)


def _read_subagent_handler(args: dict[str, Any], **kwargs: Any) -> str:
    mgr = get_manager()
    agent_id = str(args.get("agent_id") or "").strip()
    if not agent_id:
        return "Error: 'agent_id' is required."
    block = bool(args.get("block", False))
    timeout = int(args.get("timeout", 30) or 30)
    try:
        out = mgr.read(agent_id, block=block, timeout=timeout)
    except SubagentError as exc:
        return f"Error: {exc}"
    return json.dumps(out, ensure_ascii=False)


def _any_backend_available() -> bool:
    """True if a devin or codex binary is resolvable.

    Uses robust resolution (config/env/PATH/well-known locations) rather than a
    bare ``shutil.which`` so the tools stay visible even when Hermes is launched
    by a GUI ACP client with a minimal PATH (which doesn't include ~/.local/bin,
    Homebrew, or nvm node dirs). Without this, the model only sees built-in
    ``delegate_task`` and not ``run_subagent`` / ``read_subagent``.
    """
    try:
        cfg = config.load_config()
    except Exception:
        cfg = None
    return bool(
        binaries.resolve_backend_binary("devin", cfg)
        or binaries.resolve_backend_binary("codex", cfg)
    )


def contribute(ctx: Any) -> None:
    cfg = config.load_config()
    ctx.register_tool(
        name="run_subagent",
        toolset=TOOLSET,
        schema=tools_schema.run_subagent_schema(cfg),
        handler=_run_subagent_handler,
        check_fn=_any_backend_available,
        is_async=False,
        description=tools_schema.RUN_SUBAGENT_DESCRIPTION,
        emoji="\U0001F916",  # robot
    )
    ctx.register_tool(
        name="read_subagent",
        toolset=TOOLSET,
        schema=tools_schema.read_subagent_schema(),
        handler=_read_subagent_handler,
        check_fn=_any_backend_available,
        is_async=False,
        description=tools_schema.READ_SUBAGENT_DESCRIPTION,
        emoji="\U0001F4EC",  # mailbox
    )
