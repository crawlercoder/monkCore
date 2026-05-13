"""Public job schema + helpers for the ``jobs`` DynamoDB table.

The table itself is free-form (created by
:meth:`app.db.dynamodb.JobsRepository.create_job`); this module defines
the **shape the HTTP/API layer guarantees to clients**:

    {
        "job_id":      "...",
        "org_id":      "...",
        "spec":        "...",
        "status":      "CREATED | PROCESSING | COMPLETED | FAILED | CANCELLED",
        "mr_url":      "...",
        "staging_url": "...",
        "logs":        [ ... ],
        "created_at":  "..."
    }

New jobs are stored with ``status: "CREATED"`` (and ``created_at``). The
pipeline then uses ``running | succeeded | failed | awaiting_human``.
Legacy rows may still have ``pending`` for the pre-create state. The
HTTP layer maps all of these to :class:`JobStatus` via
:data:`STATUS_DB_TO_API` / :func:`api_status`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Final, List

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.orgs import OrgId


# --------------------------------------------------------------------------- #
# Primitive types                                                             #
# --------------------------------------------------------------------------- #


# URL-safe so job ids can live in paths unescaped. ``uuid4().hex`` is 32
# chars; the upper bound is 64 to leave room for caller-provided ids.
JobId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_\-]+$",
        strip_whitespace=True,
    ),
]

# Free-form product spec. Bound the max length so a single mis-paste
# can't blow the DynamoDB item-size cap (400 KB).
SpecText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=20000, strip_whitespace=True),
]


# --------------------------------------------------------------------------- #
# Status                                                                      #
# --------------------------------------------------------------------------- #


class JobStatus(str, Enum):
    """Public (API-facing) job lifecycle.

    The set is small — mapping any internal lowercase status onto one of
    these keeps UI and clients stable.
    """

    CREATED = "CREATED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# DB-layer status strings (lowercased before lookup in :func:`api_status`):
# ``CREATED | pending | running | succeeded | failed | awaiting_human``.
#
# Mapping rules:
# * ``created`` / ``CREATED`` — row just inserted; worker not started → CREATED.
# * ``pending``            — legacy same as above → CREATED.
# * ``running``            — pipeline working → PROCESSING.
# * ``awaiting_human``     — clarifying question → PROCESSING.
# * ``succeeded``          → COMPLETED.
# * ``failed``             → FAILED.
# * ``cancelled``          → CANCELLED.
STATUS_DB_TO_API: Final[dict[str, JobStatus]] = {
    "created": JobStatus.CREATED,
    "pending": JobStatus.CREATED,
    "running": JobStatus.PROCESSING,
    "awaiting_human": JobStatus.PROCESSING,
    "succeeded": JobStatus.COMPLETED,
    "failed": JobStatus.FAILED,
    "cancelled": JobStatus.CANCELLED,
}


def api_status(db_status: str | None) -> JobStatus:
    """Coerce a stored status into one of the public :class:`JobStatus` labels.

    Unknown / missing values collapse to :attr:`JobStatus.FAILED` so
    the UI never has to render an undocumented string — observability
    into the underlying oddity comes through logs, not the response.
    """
    if not db_status:
        return JobStatus.FAILED
    return STATUS_DB_TO_API.get(str(db_status).strip().lower(), JobStatus.FAILED)


# --------------------------------------------------------------------------- #
# Job schema                                                                  #
# --------------------------------------------------------------------------- #


class Job(BaseModel):
    """Canonical API representation of a job.

    ``logs`` is **not** persisted on the DynamoDB row — it's a tail of
    the per-job pipeline log rendered from
    :func:`app.artifact_manager.read_logs`. Anything beyond a few
    hundred KB would bust the DynamoDB item limit; keeping logs on
    disk also lets the worker append as it runs without write
    amplification.

    ``created_at`` is an ISO-8601 string on the wire; Pydantic
    parses/serializes via :class:`datetime`.
    """

    model_config = ConfigDict(extra="ignore")

    job_id: JobId
    org_id: OrgId
    spec: str = Field(default="", description="Original spec submitted at create time")
    status: JobStatus = Field(
        ...,
        description="CREATED | PROCESSING | COMPLETED | FAILED | CANCELLED",
    )
    mr_url: str = Field(
        default="",
        description="Merge-request URL of the first successful repo, or empty.",
    )
    staging_url: str = Field(
        default="",
        description="Staging deployment URL, or empty before it's published.",
    )
    logs: List[str] = Field(
        default_factory=list,
        description="Tail of the per-job pipeline log (newest lines last).",
    )
    created_at: datetime = Field(
        ...,
        description="ISO-8601 UTC timestamp stamped by create_job.",
    )


class JobSummary(BaseModel):
    """List-row shape for :meth:`~app.db.dynamodb.JobsRepository.list_jobs_by_org`.

    No ``logs`` — only fields stored on the DynamoDB row.
    """

    model_config = ConfigDict(extra="ignore")

    job_id: JobId
    org_id: OrgId
    spec: str = Field(default="", description="Original spec (may be long)")
    status: JobStatus
    mr_url: str = Field(default="")
    staging_url: str = Field(default="")
    created_at: datetime


__all__ = [
    "Job",
    "JobId",
    "JobStatus",
    "JobSummary",
    "STATUS_DB_TO_API",
    "SpecText",
    "api_status",
]
