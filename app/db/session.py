"""Database lifecycle hooks.

Called from the FastAPI ``lifespan`` in :mod:`app.main`:

* :func:`init_db` — bootstrap connections, register readiness probes, and
  (in local environments only) create any missing tables.
* :func:`close_db` — tear connections down on shutdown.
"""

from __future__ import annotations

from app.config import Environment, Settings
from app.db import dynamodb as jobs_tbl
from app.db import orgs as orgs_tbl
from app.db import repos as repos_tbl
from app.logging import get_logger
from app.services.readiness import register_probe

log = get_logger(__name__)


async def init_db(settings: Settings) -> None:
    """Initialize database connections and readiness probes."""
    if settings.database_url:
        log.info("init_db: sql database configured at %s", _redact_dsn(settings.database_url))

    if settings.environment == Environment.LOCAL:
        for name, init in (
            ("jobs", jobs_tbl.init_table),
            ("orgs", orgs_tbl.init_table),
            ("repos", repos_tbl.init_table),
        ):
            try:
                await init(settings)
            except jobs_tbl.DynamoDBError as exc:
                log.warning("init_db: could not auto-create '%s' table: %s", name, exc)

    register_probe("dynamodb_jobs", lambda: jobs_tbl.ping(settings))
    register_probe("dynamodb_orgs", lambda: orgs_tbl.ping(settings))
    register_probe("dynamodb_repos", lambda: repos_tbl.ping(settings))


async def close_db() -> None:
    """Tear down database connections."""
    log.info("close_db: noop")


def _redact_dsn(dsn: str) -> str:
    """Hide passwords in database URLs before logging."""
    if "://" not in dsn or "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return dsn
