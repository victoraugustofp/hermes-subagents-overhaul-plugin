"""Plugin contribution framework.

Each ``hermes_subagents_overhaul/contrib/<name>.py`` defines::

    def contribute(ctx) -> None:   # ctx is Hermes' PluginContext
        ...

Discovery is automatic at ``register(ctx)`` time. A contributor that raises is
isolated and logged; the others still run. Never edit
``hermes_subagents_overhaul/__init__.py`` or another contributor's module.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import traceback
from typing import Any

logger = logging.getLogger("hermes_subagents_overhaul.contrib")


def run_contributors(ctx: Any) -> list[str]:
    """Import every non-underscore module here and call its ``contribute(ctx)``."""
    done: list[str] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except Exception:
            logger.warning("contrib '%s' import failed:\n%s", info.name, traceback.format_exc())
            continue
        fn = getattr(module, "contribute", None)
        if fn is None:
            continue
        try:
            fn(ctx)
            done.append(info.name)
        except Exception:
            logger.warning("contrib '%s' failed:\n%s", info.name, traceback.format_exc())
    if done:
        logger.info("hermes-subagents-overhaul contributors ran: %s", ", ".join(done))
    return done
