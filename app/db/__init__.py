"""Persistence layer: DynamoDB for workflow state, optional SQL for relational data.

One module per table under :mod:`app.db`:

* :mod:`app.db.dynamodb` — ``jobs`` table + shared primitives (resource
  factory, update-expression builder, error wrapping). Every sibling
  module reuses these helpers.
* :mod:`app.db.orgs`    — ``orgs`` table.
* :mod:`app.db.repos`   — ``repos`` table (with org_id GSI).
"""

from app.db.dynamodb import (
    DynamoDBError,
    JobAlreadyExistsError,
    JobNotFoundError,
    JobsRepository,
)
from app.db.dynamodb import init_table as init_jobs_table
from app.db.dynamodb import ping as ping_jobs
from app.db.orgs import OrgAlreadyExistsError, OrgNotFoundError, OrgsRepository
from app.db.orgs import init_table as init_orgs_table
from app.db.orgs import ping as ping_orgs
from app.db.repos import RepoAlreadyExistsError, RepoNotFoundError, ReposRepository
from app.db.repos import init_table as init_repos_table
from app.db.repos import ping as ping_repos

__all__ = [
    "DynamoDBError",
    # jobs
    "JobAlreadyExistsError",
    "JobNotFoundError",
    "JobsRepository",
    "init_jobs_table",
    "ping_jobs",
    # orgs
    "OrgAlreadyExistsError",
    "OrgNotFoundError",
    "OrgsRepository",
    "init_orgs_table",
    "ping_orgs",
    # repos
    "RepoAlreadyExistsError",
    "RepoNotFoundError",
    "ReposRepository",
    "init_repos_table",
    "ping_repos",
]
