"""Pydantic models for the ``repos`` DynamoDB table.

A repo belongs to exactly one org (``org_id`` is the foreign key) and moves
through three states: ``PENDING`` → ``CLONING`` → ``READY``. The cloning
pipeline (see the git sync service) owns the transitions.

Three shapes:

* :class:`RepoCreate` — input (``repo_id`` optional; status defaults to PENDING).
* :class:`RepoUpdate` — patch; every field optional.
* :class:`Repo`       — full record as stored/returned.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints, field_validator

from app.models.orgs import OrgId


class RepoStatus(str, Enum):
    """Lifecycle states for a repo.

    The values are stored as plain strings in DynamoDB and re-hydrated via
    Pydantic's native ``Enum`` parsing.
    """

    PENDING = "PENDING"   # created, not yet picked up by the cloner
    CLONING = "CLONING"   # cloner is actively fetching to local disk / EFS
    READY = "READY"       # local checkout is fresh and usable


RepoId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_\-]+$",
        strip_whitespace=True,
    ),
]


# Git branch names: see `git check-ref-format`. We enforce a conservative
# subset that's safe to embed in URLs, file paths, and shell commands.
BranchName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_][A-Za-z0-9._/\-]*$",
    ),
]


# Covers:
#   https://github.com/acme/repo(.git)      — HTTPS
#   git@github.com:acme/repo(.git)          — SSH shorthand
#   ssh://git@github.com[:port]/acme/repo   — explicit SSH
_GIT_URL_RE = re.compile(
    r"""
    ^
    (?:
        https?://[\w.\-]+(?::\d+)?/[\w./\-]+?(?:\.git)?    # http(s)://host[:port]/path
      | [\w.\-]+@[\w.\-]+:[\w./\-]+?(?:\.git)?            # user@host:path
      | ssh://[\w.\-]+@[\w.\-]+(?::\d+)?/[\w./\-]+?(?:\.git)?  # ssh://user@host/path
    )
    $
    """,
    re.VERBOSE,
)


def _validate_git_url(v: str) -> str:
    v = v.strip()
    if not _GIT_URL_RE.match(v):
        raise ValueError(f"not a valid git URL: {v!r}")
    return v


def _validate_branch(v: str) -> str:
    # Complement the regex with a few extra git rules the regex can't express.
    if ".." in v or v.endswith((".lock", "."))  or "//" in v or v.startswith("-"):
        raise ValueError(f"invalid git branch name: {v!r}")
    return v


# Annotated type so HTTP request models and DB models enforce the same URL
# rule without duplicating the validator.
RepoUrl = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2048),
    AfterValidator(_validate_git_url),
]


class RepoBase(BaseModel):
    """Fields shared by create/read. Update has its own (all-optional) shape."""

    model_config = ConfigDict(extra="forbid")

    repo_url: RepoUrl
    org_id: OrgId
    branch: BranchName = "main"

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, v: str) -> str:
        return _validate_branch(v)


class RepoCreate(RepoBase):
    """Input payload for :meth:`ReposRepository.create`."""

    repo_id: Optional[RepoId] = None
    status: RepoStatus = RepoStatus.PENDING


class RepoUpdate(BaseModel):
    """Partial patch."""

    model_config = ConfigDict(extra="forbid")

    repo_url: Optional[RepoUrl] = None
    branch: Optional[BranchName] = None
    status: Optional[RepoStatus] = None

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, v):
        return _validate_branch(v) if v is not None else v


class Repo(RepoBase):
    """Full representation as stored in and returned from DynamoDB."""

    repo_id: RepoId
    status: RepoStatus
    created_at: datetime
    updated_at: datetime


__all__ = [
    "BranchName",
    "Repo",
    "RepoBase",
    "RepoCreate",
    "RepoId",
    "RepoStatus",
    "RepoUpdate",
    "RepoUrl",
]
