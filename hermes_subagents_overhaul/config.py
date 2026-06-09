"""Profile resolution + plugin configuration.

A *profile* maps a Devin-style profile name (``subagent_explore``, ``coder``, ...)
onto a concrete **backend + model + sandbox/permission posture + launch flags**.
There is intentionally NO ``backend`` argument on the tool — the profile selects it,
keeping the ``run_subagent`` schema identical to Devin's published spec.

Built-in defaults mirror Devin's named profiles (PLAN.md §3.3); user config under
``~/.hermes/config.yaml`` (``subagents:`` section) overrides/extends them.

This module has no third-party deps beyond ``pyyaml`` (always present with Hermes)
and is fully unit-testable by passing an explicit ``cfg`` dict.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any

# Recognised backend names.
BACKEND_CODEX = "codex"
BACKEND_DEVIN = "devin"
KNOWN_BACKENDS = (BACKEND_CODEX, BACKEND_DEVIN)

# Sandbox vocabulary is Codex's (read-only | workspace-write | danger-full-access);
# the Devin backend maps these onto its own --permission-mode / --sandbox posture.
SANDBOX_READ_ONLY = "read-only"
SANDBOX_WORKSPACE_WRITE = "workspace-write"
SANDBOX_DANGER = "danger-full-access"


class ProfileError(ValueError):
    """Raised when an unknown profile is requested. Message lists valid choices."""


@dataclass(frozen=True)
class ResolvedProfile:
    """A fully-resolved launch spec for one subagent run."""

    name: str
    backend: str                      # "codex" | "devin"
    model: str | None = None
    sandbox: str | None = None        # read-only | workspace-write | danger-full-access
    read_only: bool = False
    permission_mode: str | None = None  # devin: "auto" | "dangerous"
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""

    def with_overrides(self, **kw: Any) -> "ResolvedProfile":
        return replace(self, **kw)


# Top-level subagents.* defaults (everything but `profiles`).
DEFAULT_SETTINGS: dict[str, Any] = {
    "default_backend": BACKEND_CODEX,
    "workspace": "auto",            # "auto" -> parent cwd ($TERMINAL_CWD / cwd)
    "max_background": 4,            # cap on concurrently-running background subagents
    "max_foreground": 1,           # Devin-style: one foreground subagent at a time
    "notify_via_inject": True,     # best-effort proactive wake (§8.3)
    "throttle_seconds": 1.0,       # coalesce background tool_call_update emissions
}

# Built-in profiles (PLAN.md §3.3). User config is merged on top per-profile.
DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "subagent_explore": {
        "backend": BACKEND_CODEX,
        "model": "gpt-5.5",
        "sandbox": SANDBOX_READ_ONLY,
        "read_only": True,
        "description": "Read-only research / exploration (no writes).",
    },
    "subagent_general": {
        "backend": BACKEND_CODEX,
        "model": "gpt-5.5",
        "sandbox": SANDBOX_WORKSPACE_WRITE,
        "description": "General-purpose subagent with workspace write access.",
    },
    "coder": {
        "backend": BACKEND_DEVIN,
        "model": "gpt",
        "sandbox": SANDBOX_WORKSPACE_WRITE,
        "description": "Implement features / fix bugs (Devin).",
    },
    "debugger": {
        "backend": BACKEND_CODEX,
        "model": "gpt-5.5",
        "sandbox": SANDBOX_WORKSPACE_WRITE,
        "description": "Systematic debugging / root-cause analysis.",
    },
    "frontend-developer": {
        "backend": BACKEND_DEVIN,
        "model": "opus",
        "sandbox": SANDBOX_WORKSPACE_WRITE,
        "description": "Frontend / UI work (Devin).",
    },
}


def _hermes_config_path() -> str | None:
    """Best-effort path to ~/.hermes/config.yaml (profile-aware if Hermes is importable)."""
    try:  # Hermes exposes a profile-aware home resolver.
        from hermes_constants import get_hermes_home  # type: ignore

        home = get_hermes_home()
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    path = os.path.join(home, "config.yaml")
    return path if os.path.isfile(path) else None


def load_config(explicit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the merged ``subagents`` config block.

    ``explicit`` (tests / callers) takes precedence over the on-disk YAML. The
    result always has ``profiles`` and every key in :data:`DEFAULT_SETTINGS`.
    """
    raw: dict[str, Any] = {}
    if explicit is not None:
        raw = dict(explicit)
    else:
        path = _hermes_config_path()
        if path:
            try:
                import yaml  # type: ignore

                with open(path, "r", encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh) or {}
                raw = dict(doc.get("subagents") or {})
            except Exception:
                raw = {}

    cfg: dict[str, Any] = dict(DEFAULT_SETTINGS)
    for k, v in raw.items():
        if k != "profiles":
            cfg[k] = v

    profiles: dict[str, dict[str, Any]] = {n: dict(p) for n, p in DEFAULT_PROFILES.items()}
    for name, spec in (raw.get("profiles") or {}).items():
        base = dict(profiles.get(name, {}))
        base.update(spec or {})
        profiles[name] = base
    cfg["profiles"] = profiles
    return cfg


def available_profiles(cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg if cfg is not None else load_config()
    return sorted(cfg.get("profiles", {}).keys())


def resolve_profile(name: str, cfg: dict[str, Any] | None = None) -> ResolvedProfile:
    """Resolve ``name`` to a :class:`ResolvedProfile`, or raise :class:`ProfileError`."""
    cfg = cfg if cfg is not None else load_config()
    profiles = cfg.get("profiles", {})
    if name not in profiles:
        choices = ", ".join(sorted(profiles)) or "(none configured)"
        raise ProfileError(f"Unknown profile '{name}'. Available profiles: {choices}.")

    spec = dict(profiles[name])
    backend = str(spec.get("backend") or cfg.get("default_backend") or BACKEND_CODEX)
    if backend not in KNOWN_BACKENDS:
        raise ProfileError(
            f"Profile '{name}' uses unknown backend '{backend}'. "
            f"Known backends: {', '.join(KNOWN_BACKENDS)}."
        )
    return ResolvedProfile(
        name=name,
        backend=backend,
        model=spec.get("model"),
        sandbox=spec.get("sandbox"),
        read_only=bool(spec.get("read_only", spec.get("sandbox") == SANDBOX_READ_ONLY)),
        permission_mode=spec.get("permission_mode"),
        extra_args=list(spec.get("extra_args") or []),
        env=dict(spec.get("env") or {}),
        description=str(spec.get("description") or ""),
    )
