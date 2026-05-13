"""Background workers.

Each worker here is a self-contained coroutine or class that processes
one unit of work (a DB row, an event, a message). Workers are designed
to be invoked by whatever scheduling layer the deployment uses —
EventBridge→Lambda, SQS→ECS, cron, or an ad-hoc CLI — without carrying
any framework-specific coupling.

Exports
-------
* :func:`process_repo`        — ingest a single repo (clone + mark READY).
* :class:`RepoIngestWorker`   — injectable class form of the above.
* :class:`RepoIngestSkipped`  — raised when a row is already terminal.
* :class:`Event`, :class:`EventHandler` — shared EventBridge envelope
  and abstract handler (see :mod:`app.workers.base`).
"""

from app.workers.base import Event, EventHandler
from app.workers.repo_ingest import (
    RepoIngestError,
    RepoIngestSkipped,
    RepoIngestWorker,
    process_repo,
)

__all__ = [
    "Event",
    "EventHandler",
    "RepoIngestError",
    "RepoIngestSkipped",
    "RepoIngestWorker",
    "process_repo",
]
