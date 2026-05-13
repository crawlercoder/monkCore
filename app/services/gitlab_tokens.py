"""GitLab token management, per-org.

Each org owns exactly one secret in AWS Secrets Manager, stored as JSON::

    { "gitlab_token": "glpat-..." }

The secret *name* lives on the org record in DynamoDB (``orgs.secret_name``);
the secret *value* lives only in Secrets Manager. This module composes
those two stores so callers only ever deal with org ids, never raw names.

Flow (writes)::

    create_secret(org_id, token)
        1. Fetch org by org_id from DynamoDB.
        2. Upsert {"gitlab_token": token} at org.secret_name.
        3. Invalidate the cached copy — rotations take effect immediately.

Flow (reads)::

    get_gitlab_token_for_org(org_id)
        1. Fetch org by org_id from DynamoDB.
        2. Read org.secret_name from Secrets Manager (TTL cache, default 5m).
        3. Validate the {"gitlab_token": "..."} schema and return the token.

Caching
-------
Reads flow through :class:`SecretsManagerClient`'s TTL cache. Writes call
``invalidate`` on the cache for the affected name so the next reader
never sees a stale token. Concurrent readers for the same secret share a
single upstream fetch (per-key lock in the secrets client).

Errors
------
* :class:`OrgNotFoundError`       — org_id is unknown.
* :class:`SecretFormatError`      — the stored JSON isn't ``{"gitlab_token": "..."}``.
* :class:`SecretAccessDeniedError`, :class:`SecretDecryptError`,
  :class:`SecretsManagerError` — underlying Secrets Manager failures,
  bubbled up unchanged so callers can catch them with one try/except.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from app.config import GitlabTokenStorage, Settings, get_settings
from app.db.orgs import OrgNotFoundError, OrgsRepository
from app.logging import get_logger, log_event
from app.services.secrets import (
    SecretFormatError,
    SecretNotFoundError,
    SecretsManagerClient,
    get_secrets_client,
)

log = get_logger(__name__)


def _org_id_from_secret_name(secret_name: str) -> Optional[str]:
    """Parse ``orgs/<org_id>/gitlab-token`` for stable local file names."""
    parts = [p for p in secret_name.split("/") if p]
    if len(parts) >= 3 and parts[0] == "orgs" and parts[-1] == "gitlab-token":
        return parts[1]
    return None


def _local_token_path(settings: Settings, secret_name: str) -> Path:
    root = Path(settings.workspace_root).expanduser().resolve()
    directory = root / ".local-gitlab-tokens"
    org_id = _org_id_from_secret_name(secret_name)
    key = org_id or hashlib.sha256(secret_name.encode("utf-8")).hexdigest()[:32]
    return directory / f"{key}.json"


def _write_local_token_file(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    path.write_text(body, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


class GitlabSecret(BaseModel):
    """Schema of the stored secret.

    ``extra="ignore"`` (Pydantic default) is deliberate: the bundle may
    grow fields later (rotated_at, scopes, expires_at, …) and older code
    should keep reading ``gitlab_token`` without crashing.
    """

    gitlab_token: str = Field(min_length=1)


class GitlabTokensService:
    """Thin composition of :class:`OrgsRepository` + :class:`SecretsManagerClient`.

    Construct with defaults for production; inject mocks or non-default
    clients in tests. Repositories are cheap to instantiate — boto resource
    caches are shared across instances.
    """

    def __init__(
        self,
        orgs_repo: Optional[OrgsRepository] = None,
        secrets_client: Optional[SecretsManagerClient] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self._orgs = orgs_repo or OrgsRepository()
        self._secrets_client = secrets_client
        self._settings = settings or get_settings()

    async def _secrets(self) -> SecretsManagerClient:
        if self._secrets_client is None:
            self._secrets_client = await get_secrets_client()
        return self._secrets_client

    def _use_local_token_files(self) -> bool:
        return self._settings.gitlab_token_storage is GitlabTokenStorage.LOCAL

    # ------------------------------------------------------------------ #
    # Writes                                                             #
    # ------------------------------------------------------------------ #

    async def create_secret(self, org_id: str, token: str) -> str:
        """Store or rotate the GitLab token for ``org_id``.

        Idempotent: calling twice with different tokens rotates the value
        at the same secret name. Returns the secret name.
        """
        if not org_id:
            raise ValueError("org_id must be a non-empty string")
        if not token or not token.strip():
            raise ValueError("token must be non-empty")

        org = await self._orgs.get(org_id)
        if org is None:
            raise OrgNotFoundError(f"org '{org_id}' not found")

        payload = GitlabSecret(gitlab_token=token).model_dump()
        if self._use_local_token_files():
            body = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True)
            path = _local_token_path(self._settings, org.secret_name)
            await asyncio.to_thread(_write_local_token_file, path, body)
            log_event(
                log, "gitlab_tokens.local_stored",
                "gitlab token written to local file",
                org_id=org_id, secret_name=org.secret_name,
            )
            return org.secret_name
        secrets = await self._secrets()
        await secrets.put_secret_json(
            org.secret_name,
            payload,
            description=f"GitLab token for org {org_id}",
        )
        log_event(log, "gitlab_tokens.secret_stored",
                  "gitlab token written to Secrets Manager",
                  org_id=org_id, secret_name=org.secret_name)
        return org.secret_name

    async def put_secret_direct(
        self,
        secret_name: str,
        token: str,
        *,
        description: Optional[str] = None,
    ) -> str:
        """Write a secret to a known name without going through DynamoDB.

        Used by :class:`RegistryService.create_org`, which writes the
        token *before* the org row exists so the "org → reachable secret"
        invariant holds.
        """
        if not secret_name:
            raise ValueError("secret_name must be a non-empty string")
        if not token or not token.strip():
            raise ValueError("token must be non-empty")

        payload = GitlabSecret(gitlab_token=token).model_dump()
        if self._use_local_token_files():
            body = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True)
            path = _local_token_path(self._settings, secret_name)
            await asyncio.to_thread(_write_local_token_file, path, body)
            log_event(
                log, "gitlab_tokens.local_stored",
                "gitlab token written to local file (pre-org)",
                secret_name=secret_name,
            )
            return secret_name
        secrets = await self._secrets()
        return await secrets.put_secret_json(
            secret_name, payload, description=description
        )

    # ------------------------------------------------------------------ #
    # Reads                                                              #
    # ------------------------------------------------------------------ #

    async def get_secret(
        self,
        secret_name: str,
        *,
        force_refresh: bool = False,
    ) -> GitlabSecret:
        """Fetch and validate a GitLab secret by name.

        Use :meth:`get_gitlab_token_for_org` when you start from an org
        id; this method exists for cases where you already resolved the
        name (for example, a rotation Lambda invoked with it).
        """
        if not secret_name:
            raise ValueError("secret_name must be a non-empty string")

        if self._use_local_token_files():
            path = _local_token_path(self._settings, secret_name)

            def _read() -> dict:
                if not path.is_file():
                    raise SecretNotFoundError(
                        f"local token file for '{secret_name}' not found: {path}"
                    )
                raw = path.read_text(encoding="utf-8")
                return json.loads(raw)

            payload = await asyncio.to_thread(_read)
        else:
            secrets = await self._secrets()
            payload = await secrets.get_secret_json(
                secret_name, force_refresh=force_refresh
            )
        try:
            return GitlabSecret.model_validate(payload)
        except ValidationError as exc:
            # Wrap Pydantic's verbose error so we never leak payload
            # contents — the raw ValidationError embeds offending values.
            raise SecretFormatError(
                f"secret '{secret_name}' is not a valid GitLab secret "
                "(expected a JSON object with a non-empty 'gitlab_token')"
            ) from exc

    async def get_gitlab_token_for_org(
        self,
        org_id: str,
        *,
        force_refresh: bool = False,
    ) -> str:
        """Resolve org → secret_name → gitlab_token.

        ``force_refresh=True`` bypasses the cache for the next read, which
        is the right thing to do immediately after a rotation event.
        """
        if not org_id:
            raise ValueError("org_id must be a non-empty string")

        org = await self._orgs.get(org_id)
        if org is None:
            raise OrgNotFoundError(f"org '{org_id}' not found")

        secret = await self.get_secret(org.secret_name, force_refresh=force_refresh)
        return secret.gitlab_token


# --------------------------------------------------------------------------- #
# Module-level convenience wrappers (share the singleton service)             #
# --------------------------------------------------------------------------- #

_service: Optional[GitlabTokensService] = None


def _get_service() -> GitlabTokensService:
    global _service
    if _service is None:
        _service = GitlabTokensService()
    return _service


async def create_secret(org_id: str, token: str) -> str:
    """Store or rotate the GitLab token for ``org_id``. Returns the secret name."""
    return await _get_service().create_secret(org_id, token)


async def get_secret(
    secret_name: str,
    *,
    force_refresh: bool = False,
) -> GitlabSecret:
    """Fetch and validate a GitLab secret by name."""
    return await _get_service().get_secret(secret_name, force_refresh=force_refresh)


async def get_gitlab_token_for_org(
    org_id: str,
    *,
    force_refresh: bool = False,
) -> str:
    """Resolve org → secret_name → gitlab_token."""
    return await _get_service().get_gitlab_token_for_org(
        org_id, force_refresh=force_refresh
    )


__all__ = [
    "GitlabSecret",
    "GitlabTokensService",
    "create_secret",
    "get_gitlab_token_for_org",
    "get_secret",
]
