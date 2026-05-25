from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity

from models import CheckContext


@activity.defn(name="activities.custom_checks.run_all")
async def run_all(ctx: CheckContext) -> dict[str, Any]:
    """Discovers and runs all dependency_scout.checks entry-point functions in parallel."""
    try:
        from importlib.metadata import entry_points

        eps = list(entry_points(group="dependency_scout.checks"))
    except Exception:
        return {}

    if not eps:
        return {}

    async def _run_one(ep) -> tuple[str, Any]:
        try:
            fn = ep.load()
            result = await fn(ctx)
            return ep.name, result
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning("Custom check %r failed: %r — skipped", ep.name, exc)
            return ep.name, None

    pairs = await asyncio.gather(*(_run_one(ep) for ep in eps))
    return {name: result for name, result in pairs if result is not None}
