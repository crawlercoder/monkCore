"""Readiness probe registry.

Downstream components (db, bedrock, dynamodb, opensearch, …) register
async `ReadinessProbe` callables with `register_probe`. `/ready` runs them
all concurrently with a short timeout and returns a per-check status.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, List, Tuple

from app.logging import get_logger
from app.models.health import ReadinessCheck

log = get_logger(__name__)

ReadinessProbe = Callable[[], Awaitable[Tuple[bool, str | None]]]

_probes: List[Tuple[str, ReadinessProbe]] = []
_PROBE_TIMEOUT_SECONDS = 2.0


def register_probe(name: str, probe: ReadinessProbe) -> None:
    """Register a named async probe. Idempotent by name."""
    for i, (n, _) in enumerate(_probes):
        if n == name:
            _probes[i] = (name, probe)
            return
    _probes.append((name, probe))


async def _run_one(name: str, probe: ReadinessProbe) -> ReadinessCheck:
    try:
        healthy, detail = await asyncio.wait_for(probe(), timeout=_PROBE_TIMEOUT_SECONDS)
        return ReadinessCheck(name=name, healthy=healthy, detail=detail)
    except asyncio.TimeoutError:
        return ReadinessCheck(name=name, healthy=False, detail="probe timed out")
    except Exception as exc:
        log.warning("readiness probe '%s' raised: %s", name, exc)
        return ReadinessCheck(name=name, healthy=False, detail=f"{type(exc).__name__}: {exc}")


async def run_readiness_checks() -> Dict[str, ReadinessCheck]:
    if not _probes:
        return {}
    results = await asyncio.gather(*(_run_one(name, p) for name, p in _probes))
    return {r.name: r for r in results}
