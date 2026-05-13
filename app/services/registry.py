"""Registry service: manage orgs and their repos.

High-level API on top of three lower-level components:

* :class:`OrgsRepository`        — DynamoDB ``orgs`` table
* :class:`ReposRepository`       — DynamoDB ``repos`` table (with org_id GSI)
* :class:`GitlabTokensService`   — AWS Secrets Manager per-org secret

Ownership rules
---------------
* IDs are generated here (``org_<hex>`` / ``repo_<hex>``), never supplied
  by callers. A single uuid4 collision-free prefix keeps log lines
  grep-friendly and prevents clients from reusing ids across orgs.
* Timestamps are ISO-8601 UTC with ``Z`` suffix, written by the
  repository layer via :func:`app.db.dynamodb.now_iso`.
* Cross-table relationships are validated before writes:
  :meth:`register_repo` refuses an unknown ``org_id``; reads of a
  missing entity raise a typed ``NotFoundError``.

Atomicity
---------
:meth:`create_org` writes the GitLab token to Secrets Manager *before*
inserting the org into DynamoDB, so the invariant **"an org row implies
a reachable secret"** always holds. If the DynamoDB write fails, the
secret is left behind and logged at ``ERROR`` for operator cleanup. We
prefer an orphaned secret (discoverable, cheap) over an org that points
at nothing.
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional, Tuple, Union

from app.config import Settings, get_settings
from app.db.orgs import OrgNotFoundError, OrgsRepository
from app.db.repos import RepoNotFoundError, ReposRepository
from app.logging import get_logger, log_context, log_event
from app.models.orgs import Org, OrgCreate
from app.models.repos import Repo, RepoCreate, RepoStatus, RepoUpdate
from app.services.gitlab_tokens import GitlabTokensService

log = get_logger(__name__)

_ORG_ID_PREFIX = "org_"
_REPO_ID_PREFIX = "repo_"


def _new_org_id() -> str:
    return f"{_ORG_ID_PREFIX}{uuid.uuid4().hex}"


def _new_repo_id() -> str:
    return f"{_REPO_ID_PREFIX}{uuid.uuid4().hex}"


class RegistryService:
    """Compose orgs + repos + GitLab tokens behind a single façade."""

    def __init__(
        self,
        orgs_repo: Optional[OrgsRepository] = None,
        repos_repo: Optional[ReposRepository] = None,
        tokens_service: Optional[GitlabTokensService] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._orgs = orgs_repo or OrgsRepository(self._settings)
        self._repos = repos_repo or ReposRepository(self._settings)
        self._tokens = tokens_service or GitlabTokensService(orgs_repo=self._orgs)

    # ================================================================== #
    # Orgs                                                                #
    # ================================================================== #

    async def create_org(self, name: str, token: str) -> Org:
        """Create an org and store its GitLab token.

        Returns the full :class:`Org` (with server-generated ``org_id``,
        ``secret_name``, ``created_at``, ``updated_at``).
        """
        if not name or not name.strip():
            raise ValueError("name must be non-empty")
        if not token or not token.strip():
            raise ValueError("token must be non-empty")

        org_id = _new_org_id()
        secret_name = self._settings.secrets_manager.secret_name_for(org_id)

        # Bind org_id eagerly so both the secret write and the DB write
        # produce logs tagged with the id, even though the row doesn't
        # exist yet.
        with log_context(org_id=org_id):
            # 1. Store the secret first so "org row exists ⇒ secret
            #    exists" remains true under any failure point.
            await self._tokens.put_secret_direct(
                secret_name,
                token,
                description=f"GitLab token for org {org_id}",
            )

            # 2. Create the org record. If this fails, the secret we
            #    just wrote is orphaned: log loudly so an operator can
            #    clean up.
            try:
                org = await self._orgs.create(
                    OrgCreate(org_id=org_id, name=name.strip(), secret_name=secret_name)
                )
            except Exception:
                log_event(
                    log, "registry.create_org.orphaned_secret",
                    "DynamoDB write failed after secret was written "
                    "— orphaned secret must be cleaned up manually",
                    level=logging.ERROR,
                    exc_info=True,
                    secret_name=secret_name,
                )
                raise

            log_event(log, "registry.create_org", "org provisioned",
                      name=name.strip(), secret_name=secret_name)
            return org

    async def create_or_get_org(self, name: str, token: str) -> Tuple[Org, bool]:
        """Create an org, or reuse one whose normalized name already exists.

        If :meth:`OrgsRepository.find_by_name` returns a row, the submitted
        GitLab token is written to that org's secret (rotation) and the
        existing :class:`Org` is returned with ``reused=True``. Otherwise
        this delegates to :meth:`create_org` and returns ``reused=False``.

        The HTTP layer maps ``reused`` to 200 vs 201; see
        :func:`app.api.v1.orgs.create_org`.
        """
        if not name or not str(name).strip():
            raise ValueError("name must be non-empty")
        if not token or not str(token).strip():
            raise ValueError("token must be non-empty")

        existing = await self._orgs.find_by_name(name)
        if existing is not None:
            with log_context(org_id=existing.org_id):
                await self._tokens.create_secret(existing.org_id, token)
                org = await self._orgs.get(existing.org_id)
                if org is None:
                    raise RuntimeError(
                        f"org '{existing.org_id}' missing after find_by_name match"
                    )
                log_event(
                    log,
                    "registry.create_or_get_org",
                    "reused org by name — token rotated",
                    name=str(name).strip(),
                )
                return org, True

        org = await self.create_org(name, token)
        return org, False

    async def get_org(self, org_id: str) -> Org:
        """Fetch an org by id. Raises :class:`OrgNotFoundError` if missing."""
        if not org_id:
            raise ValueError("org_id must be a non-empty string")
        org = await self._orgs.get(org_id)
        if org is None:
            raise OrgNotFoundError(f"org '{org_id}' not found")
        return org

    async def list_orgs(self, *, limit: int = 100) -> List[Org]:
        """Return up to ``limit`` orgs. Thin pass-through to the repo."""
        return await self._orgs.list_all(limit=limit)

    # ================================================================== #
    # Repos                                                               #
    # ================================================================== #

    async def register_or_get_repo(
        self,
        repo_url: str,
        org_id: str,
        *,
        branch: str = "main",
    ) -> Tuple[Repo, bool]:
        """Register a repo or return an existing row for the same org + URL.

        ``(org_id, repo_url)`` is idempotent after :class:`RepoCreate`
        validation (strip + git-URL rules): if a row already exists, it is
        returned with ``reused=True`` and **not** overwritten. Otherwise
        delegates to :meth:`register_repo` (``reused=False``).

        See :func:`app.api.v1.repos.register_repo` for HTTP mapping.
        """
        if not repo_url or not str(repo_url).strip():
            raise ValueError("repo_url must be non-empty")
        if not org_id:
            raise ValueError("org_id must be a non-empty string")

        validated = RepoCreate(
            repo_url=str(repo_url).strip(),
            org_id=org_id,
            branch=branch,
            status=RepoStatus.PENDING,
        )
        await self.get_org(validated.org_id)

        existing = await self._repos.find_by_org_and_url(
            validated.org_id,
            validated.repo_url,
        )
        if existing is not None:
            with log_context(repo_id=existing.repo_id, org_id=validated.org_id):
                log_event(
                    log,
                    "registry.register_or_get_repo",
                    "reused repo by org_id + repo_url",
                    repo_url=validated.repo_url,
                )
            return existing, True

        repo = await self.register_repo(
            validated.repo_url,
            validated.org_id,
            branch=validated.branch,
        )
        return repo, False

    async def register_repo(
        self,
        repo_url: str,
        org_id: str,
        *,
        branch: str = "main",
    ) -> Repo:
        """Register a new repo under ``org_id``.

        Validates that the org exists (DynamoDB doesn't enforce FKs);
        generates a fresh ``repo_id``; status defaults to ``PENDING``.
        """
        if not repo_url or not repo_url.strip():
            raise ValueError("repo_url must be non-empty")
        if not org_id:
            raise ValueError("org_id must be a non-empty string")

        # Relationship validation: raises OrgNotFoundError if absent.
        await self.get_org(org_id)

        repo_id = _new_repo_id()
        with log_context(repo_id=repo_id, org_id=org_id):
            # RepoCreate runs URL + branch validation.
            repo = await self._repos.create(
                RepoCreate(
                    repo_id=repo_id,
                    repo_url=repo_url.strip(),
                    org_id=org_id,
                    branch=branch,
                    status=RepoStatus.PENDING,
                )
            )
            log_event(log, "registry.register_repo", "repo registered",
                      branch=branch)
            return repo

    async def get_repo(self, repo_id: str) -> Repo:
        """Fetch a repo by id. Raises :class:`RepoNotFoundError` if missing."""
        if not repo_id:
            raise ValueError("repo_id must be a non-empty string")
        repo = await self._repos.get(repo_id)
        if repo is None:
            raise RepoNotFoundError(f"repo '{repo_id}' not found")
        return repo

    async def list_repos_by_org(
        self,
        org_id: str,
        *,
        limit: int = 50,
        newest_first: bool = True,
    ) -> List[Repo]:
        """Return all repos for an org, newest first.

        An unknown ``org_id`` yields an empty list (standard repository
        semantics for reads) rather than raising — missing vs. empty is
        indistinguishable from the GSI's perspective anyway, and callers
        who care can :meth:`get_org` first.
        """
        if not org_id:
            raise ValueError("org_id must be a non-empty string")
        return await self._repos.list_by_org(
            org_id, limit=limit, newest_first=newest_first
        )

    async def update_repo_status(
        self,
        repo_id: str,
        status: Union[RepoStatus, str],
    ) -> Repo:
        """Patch a repo's status. Raises :class:`RepoNotFoundError` if missing.

        Accepts either a :class:`RepoStatus` enum or its string value;
        an invalid string raises ``ValueError`` before any network call.
        """
        if not repo_id:
            raise ValueError("repo_id must be a non-empty string")
        if isinstance(status, str):
            try:
                status = RepoStatus(status)
            except ValueError as exc:
                allowed = [s.value for s in RepoStatus]
                raise ValueError(
                    f"invalid status {status!r}; must be one of {allowed}"
                ) from exc

        with log_context(repo_id=repo_id):
            repo = await self._repos.update(repo_id, RepoUpdate(status=status))
            log_event(log, "registry.update_repo_status",
                      "repo status patched", status=status.value)
            return repo


# --------------------------------------------------------------------------- #
# Module-level convenience wrappers                                           #
# --------------------------------------------------------------------------- #

_service: Optional[RegistryService] = None


def _svc() -> RegistryService:
    global _service
    if _service is None:
        _service = RegistryService()
    return _service


async def create_org(name: str, token: str) -> Org:
    return await _svc().create_org(name, token)


async def get_org(org_id: str) -> Org:
    return await _svc().get_org(org_id)


async def list_orgs(*, limit: int = 100) -> List[Org]:
    return await _svc().list_orgs(limit=limit)


async def register_repo(repo_url: str, org_id: str, *, branch: str = "main") -> Repo:
    return await _svc().register_repo(repo_url, org_id, branch=branch)


async def get_repo(repo_id: str) -> Repo:
    return await _svc().get_repo(repo_id)


async def list_repos_by_org(
    org_id: str, *, limit: int = 50, newest_first: bool = True
) -> List[Repo]:
    return await _svc().list_repos_by_org(
        org_id, limit=limit, newest_first=newest_first
    )


async def update_repo_status(repo_id: str, status: Union[RepoStatus, str]) -> Repo:
    return await _svc().update_repo_status(repo_id, status)


__all__ = [
    "RegistryService",
    "create_org",
    "get_org",
    "get_repo",
    "list_orgs",
    "list_repos_by_org",
    "register_repo",
    "update_repo_status",
]
