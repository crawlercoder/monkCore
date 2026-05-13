"""AWS Secrets Manager client.

Features
--------
* Thin async wrapper over blocking ``boto3`` (via :func:`asyncio.to_thread`).
* In-memory TTL cache (default 5 min) keyed by secret id, with per-secret
  locks so concurrent callers for the same secret collapse into a single
  upstream fetch.
* Transparent support for JSON-string secrets — the usual pattern for
  grouping related values (``github_token`` + ``repo_url`` + ``user_email``
  stored together and rotated atomically).
* Typed errors: ``SecretNotFoundError``, ``SecretAccessDeniedError``,
  ``SecretDecryptError``, ``SecretFormatError`` all subclass
  ``SecretsManagerError``.
* Reusable singleton via :func:`get_secrets_client`, or construct a
  :class:`SecretsManagerClient` explicitly for tests / per-environment use.

Intended usage
--------------

A single JSON secret (recommended)::

    # aws secretsmanager create-secret --name prod/agent/integrations \\
    #   --secret-string '{"github_token":"ghp_…","repo_url":"…","user_email":"…"}'

    from app.services.secrets import AppSecrets
    secrets = await AppSecrets.load()
    secrets.github_token, secrets.repo_url, secrets.user_email

Single-field ad-hoc access::

    from app.services.secrets import get_secret, get_secret_field

    token = await get_secret("prod/agent/github-token")
    repo  = await get_secret_field("prod/agent/integrations", "repo_url")
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, EmailStr, Field

from app.config import SecretsBackend, Settings, get_settings
from app.logging import get_logger

log = get_logger(__name__)

_DEFAULT_MAX_ENTRIES = 128


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #


class SecretsManagerError(Exception):
    """Base class for all errors raised by this module."""


class SecretNotFoundError(SecretsManagerError):
    """The requested secret does not exist."""


class SecretAccessDeniedError(SecretsManagerError):
    """The caller is not authorized to read the secret."""


class SecretDecryptError(SecretsManagerError):
    """KMS could not decrypt the secret (key missing, disabled, or no perms)."""


class SecretFormatError(SecretsManagerError):
    """The secret's payload didn't match the expected shape (e.g. not JSON)."""


# --------------------------------------------------------------------------- #
# Inline secrets (SECRETS_BACKEND=inline, local only)                         #
# --------------------------------------------------------------------------- #

_inline_lock = threading.Lock()
_inline_blob: Dict[str, str] = {}
_inline_disk_hydrated: bool = False


def _inline_merge_file(settings: Settings) -> None:
    """Merge ``LOCAL_SECRETS_FILE`` into :data:`_inline_blob` (caller holds lock)."""
    path = settings.local_secrets_file
    if not path:
        return
    p = Path(path).expanduser()
    if not p.is_file():
        return
    raw = p.read_text(encoding="utf-8").strip() or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SecretFormatError(
            f"LOCAL_SECRETS_FILE {p} is not valid JSON"
        ) from exc
    if not isinstance(data, dict):
        raise SecretFormatError("LOCAL_SECRETS_FILE must contain a JSON object at the root")
    for key, val in data.items():
        sk = str(key)
        if isinstance(val, dict):
            _inline_blob[sk] = json.dumps(val, separators=(",", ":"), sort_keys=True)
        elif isinstance(val, str):
            _inline_blob[sk] = val
        else:
            _inline_blob[sk] = json.dumps(val, separators=(",", ":"), sort_keys=True)


def _inline_persist_file(settings: Settings) -> None:
    """Write :data:`_inline_blob` to disk (caller holds lock)."""
    path = settings.local_secrets_file
    if not path:
        return
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    out: Dict[str, Any] = {}
    for k, v in sorted(_inline_blob.items()):
        try:
            parsed = json.loads(v)
            out[k] = parsed if isinstance(parsed, dict) else v
        except json.JSONDecodeError:
            out[k] = v
    tmp.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _inline_sync_get(settings: Settings, name: str, *, force_refresh: bool) -> str:
    global _inline_disk_hydrated
    with _inline_lock:
        if settings.local_secrets_file and (force_refresh or not _inline_disk_hydrated):
            _inline_merge_file(settings)
            _inline_disk_hydrated = True
        elif not settings.local_secrets_file and not _inline_disk_hydrated:
            _inline_disk_hydrated = True
        v = _inline_blob.get(name)
        if v is None:
            raise SecretNotFoundError(f"secret '{name}' not found")
        return v


def _inline_sync_put(
    settings: Settings,
    secret_name: str,
    secret_value: str,
    _description: str,
) -> str:
    global _inline_disk_hydrated
    with _inline_lock:
        _inline_blob[secret_name] = secret_value
        _inline_disk_hydrated = True
        _inline_persist_file(settings)
    log.info(
        "put_secret_inline",
        extra={"secret_name": secret_name, "bytes": len(secret_value)},
    )
    return f"inline:{secret_name}"


# --------------------------------------------------------------------------- #
# Cache                                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class _CacheEntry:
    value: str
    expires_at: float


# --------------------------------------------------------------------------- #
# Client                                                                      #
# --------------------------------------------------------------------------- #


class SecretsManagerClient:
    """Reusable async client for AWS Secrets Manager.

    Instances are cheap; the underlying boto3 client is process-cached so
    multiple ``SecretsManagerClient`` objects share a single connection pool.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        ttl_seconds: Optional[int] = None,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._settings = settings or get_settings()
        # Default TTL flows from config (SECRETS_CACHE_TTL_SECONDS) so one
        # env var tunes every client in the process. Explicit kwargs still
        # win for tests that want a zero-TTL cache.
        resolved_ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else self._settings.secrets_manager.cache_ttl_seconds
        )
        self._ttl = max(1, int(resolved_ttl))
        self._max_entries = max(1, int(max_entries))
        self._cache: Dict[str, _CacheEntry] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    # ---- public API ------------------------------------------------------ #

    async def get_secret(
        self,
        secret_name: str,
        *,
        force_refresh: bool = False,
    ) -> str:
        """Return the raw secret string for ``secret_name``.

        Reads through the TTL cache unless ``force_refresh=True``. Concurrent
        callers on the same name share a single upstream fetch.
        """
        if not secret_name:
            raise ValueError("secret_name must be a non-empty string")

        if not force_refresh:
            cached = self._get_cached(secret_name)
            if cached is not None:
                return cached

        lock = await self._lock_for(secret_name)
        async with lock:
            # Double-check: another waiter may have populated the cache.
            if not force_refresh:
                cached = self._get_cached(secret_name)
                if cached is not None:
                    return cached

            value = await asyncio.to_thread(
                self._fetch_blocking, secret_name, force_refresh
            )
            self._put_cached(secret_name, value)
            return value

    async def get_secret_json(
        self,
        secret_name: str,
        *,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Return a JSON-decoded secret.

        Raises :class:`SecretFormatError` if the payload isn't a JSON object.
        """
        raw = await self.get_secret(secret_name, force_refresh=force_refresh)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecretFormatError(
                f"secret '{secret_name}' is not valid JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise SecretFormatError(
                f"secret '{secret_name}' JSON must be an object, got {type(parsed).__name__}"
            )
        return parsed

    async def get_secret_field(
        self,
        secret_name: str,
        field: str,
        *,
        force_refresh: bool = False,
    ) -> str:
        """Return one field from a JSON-object secret.

        Raises :class:`SecretFormatError` if the field is missing or not a string.
        """
        data = await self.get_secret_json(secret_name, force_refresh=force_refresh)
        if field not in data:
            raise SecretFormatError(
                f"secret '{secret_name}' is missing field '{field}'"
            )
        value = data[field]
        if not isinstance(value, str):
            raise SecretFormatError(
                f"field '{field}' in '{secret_name}' must be a string, "
                f"got {type(value).__name__}"
            )
        return value

    async def put_secret(
        self,
        secret_name: str,
        secret_value: str,
        *,
        description: Optional[str] = None,
    ) -> str:
        """Create the secret if missing, otherwise rotate its value.

        Upsert semantics: the same call safely runs twice with different
        values. Returns the secret ARN. After a successful write the cache
        entry for ``secret_name`` is invalidated so the next reader fetches
        the new value immediately (instead of waiting out the TTL).
        """
        if not secret_name:
            raise ValueError("secret_name must be a non-empty string")
        if not secret_value:
            raise ValueError("secret_value must be a non-empty string")

        desc = description or f"Managed by {self._settings.app_name}"

        if self._settings.secrets_backend == SecretsBackend.INLINE:
            arn = await asyncio.to_thread(
                _inline_sync_put,
                self._settings,
                secret_name,
                secret_value,
                desc,
            )
            self.invalidate(secret_name)
            return arn

        def _write() -> str:
            client = _boto_client(self._settings.aws.region)
            try:
                resp = client.create_secret(
                    Name=secret_name,
                    SecretString=secret_value,
                    Description=desc,
                )
                return resp["ARN"]
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code != "ResourceExistsException":
                    self._raise_client_error(secret_name, exc)
                # Fall through to rotate an existing secret.
                try:
                    resp = client.put_secret_value(
                        SecretId=secret_name,
                        SecretString=secret_value,
                    )
                    return resp["ARN"]
                except ClientError as inner:
                    self._raise_client_error(secret_name, inner)
            except BotoCoreError as exc:
                raise SecretsManagerError(
                    f"put_secret('{secret_name}') failed: {exc}"
                ) from exc

        arn = await asyncio.to_thread(_write)
        self.invalidate(secret_name)
        # len() is safe to log; never log the value itself.
        log.info(
            "put_secret",
            extra={"secret_name": secret_name, "bytes": len(secret_value)},
        )
        return arn

    async def put_secret_json(
        self,
        secret_name: str,
        payload: Mapping[str, Any],
        *,
        description: Optional[str] = None,
    ) -> str:
        """Serialize ``payload`` as compact JSON and upsert it."""
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        body = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True)
        return await self.put_secret(secret_name, body, description=description)

    def invalidate(self, secret_name: str) -> None:
        """Drop a single secret from the cache (e.g. after rotation)."""
        self._cache.pop(secret_name, None)

    def clear_cache(self) -> None:
        """Drop every cached secret. Use in tests or after bulk rotation."""
        self._cache.clear()

    # ---- internals ------------------------------------------------------- #

    def _get_cached(self, name: str) -> Optional[str]:
        entry = self._cache.get(name)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._cache.pop(name, None)
            return None
        return entry.value

    def _put_cached(self, name: str, value: str) -> None:
        if len(self._cache) >= self._max_entries and name not in self._cache:
            # Evict the entry closest to expiry. Cache churn here is rare
            # because `max_entries` defaults to 128 and secrets are few.
            oldest = min(self._cache.items(), key=lambda kv: kv[1].expires_at)[0]
            self._cache.pop(oldest, None)
        self._cache[name] = _CacheEntry(
            value=value,
            expires_at=time.monotonic() + self._ttl,
        )

    async def _lock_for(self, name: str) -> asyncio.Lock:
        """Return (creating if needed) a lock unique to ``name``."""
        lock = self._locks.get(name)
        if lock is not None:
            return lock
        async with self._locks_guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[name] = lock
            return lock

    def _fetch_blocking(self, name: str, force_refresh: bool = False) -> str:
        """Blocking Secrets Manager call. Invoked via ``asyncio.to_thread``."""
        if self._settings.secrets_backend == SecretsBackend.INLINE:
            return _inline_sync_get(self._settings, name, force_refresh=force_refresh)

        client = _boto_client(self._settings.aws.region)
        try:
            resp = client.get_secret_value(SecretId=name)
        except ClientError as exc:
            self._raise_client_error(name, exc)
        except BotoCoreError as exc:
            raise SecretsManagerError(
                f"get_secret_value('{name}') failed: {exc}"
            ) from exc

        if "SecretString" in resp:
            log.debug("secret '%s' fetched (string, %d chars)", name, len(resp["SecretString"]))
            return resp["SecretString"]
        if "SecretBinary" in resp:
            # Binary secrets are rare here; decode as UTF-8 so the cache API
            # stays string-only. Callers needing raw bytes can base64-encode.
            try:
                return resp["SecretBinary"].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SecretFormatError(
                    f"secret '{name}' is binary and not UTF-8 decodable"
                ) from exc
        raise SecretsManagerError(
            f"secret '{name}' response had neither SecretString nor SecretBinary"
        )

    @staticmethod
    def _raise_client_error(name: str, exc: ClientError) -> None:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        if code == "ResourceNotFoundException":
            raise SecretNotFoundError(f"secret '{name}' not found") from exc
        if code == "AccessDeniedException":
            # Covers get_secret_value, create_secret, put_secret_value, etc.
            raise SecretAccessDeniedError(
                f"access denied for secret '{name}': {msg}"
            ) from exc
        if code in {"DecryptionFailure", "KMSAccessDeniedException"}:
            raise SecretDecryptError(
                f"decryption failed for secret '{name}': {msg}"
            ) from exc
        if code == "InvalidRequestException":
            raise SecretFormatError(
                f"invalid request for secret '{name}': {msg}"
            ) from exc
        raise SecretsManagerError(
            f"secrets manager error for '{name}' [{code}]: {msg}"
        ) from exc


# --------------------------------------------------------------------------- #
# Module-level boto client + singleton                                        #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=4)
def _boto_client(region: str):
    """Process-cached boto3 Secrets Manager client.

    Credentials come from the standard boto3 chain (env vars, shared config,
    IAM role). We configure adaptive retries so a rate-limited Secrets
    Manager endpoint doesn't cascade into a cold-start storm.
    """
    cfg = BotoConfig(
        region_name=region,
        retries={"max_attempts": 5, "mode": "adaptive"},
        connect_timeout=3,
        read_timeout=10,
    )
    return boto3.client("secretsmanager", config=cfg)


_singleton: Optional[SecretsManagerClient] = None
_singleton_lock = asyncio.Lock()


async def get_secrets_client() -> SecretsManagerClient:
    """Return the process-wide :class:`SecretsManagerClient` singleton."""
    global _singleton
    if _singleton is not None:
        return _singleton
    async with _singleton_lock:
        if _singleton is None:
            _singleton = SecretsManagerClient()
        return _singleton


# ---- convenience wrappers (stateless, cached via the singleton) ---------- #


async def get_secret(secret_name: str, *, force_refresh: bool = False) -> str:
    """Read a raw secret. Convenience wrapper over the singleton client."""
    client = await get_secrets_client()
    return await client.get_secret(secret_name, force_refresh=force_refresh)


async def get_secret_json(
    secret_name: str,
    *,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Read a JSON-object secret."""
    client = await get_secrets_client()
    return await client.get_secret_json(secret_name, force_refresh=force_refresh)


async def get_secret_field(
    secret_name: str,
    field: str,
    *,
    force_refresh: bool = False,
) -> str:
    """Read a single field from a JSON-object secret."""
    client = await get_secrets_client()
    return await client.get_secret_field(
        secret_name, field, force_refresh=force_refresh
    )


async def put_secret(
    secret_name: str,
    secret_value: str,
    *,
    description: Optional[str] = None,
) -> str:
    """Upsert a string secret."""
    client = await get_secrets_client()
    return await client.put_secret(secret_name, secret_value, description=description)


async def put_secret_json(
    secret_name: str,
    payload: Mapping[str, Any],
    *,
    description: Optional[str] = None,
) -> str:
    """Upsert a JSON-object secret."""
    client = await get_secrets_client()
    return await client.put_secret_json(secret_name, payload, description=description)


def invalidate_secret(secret_name: str) -> None:
    """Drop one secret from the singleton cache."""
    if _singleton is not None:
        _singleton.invalidate(secret_name)
    try:
        s = get_settings()
    except Exception:
        return
    if s.secrets_backend != SecretsBackend.INLINE:
        return
    global _inline_disk_hydrated
    with _inline_lock:
        _inline_blob.pop(secret_name, None)
        _inline_disk_hydrated = True
        _inline_persist_file(s)


# --------------------------------------------------------------------------- #
# Typed bundle for the app's core integration secret                          #
# --------------------------------------------------------------------------- #


class AppSecrets(BaseModel):
    """The integration secret our agent needs: GitHub auth + target + PM email.

    Stored in Secrets Manager as a single JSON blob so the three values
    rotate atomically. The secret name is read from
    ``Settings.app_secret_name`` (env: ``APP_SECRET_NAME``) and falls back
    to a per-environment default.
    """

    github_token: str = Field(min_length=1)
    repo_url: str = Field(min_length=1)
    user_email: EmailStr

    @classmethod
    async def load(
        cls,
        secret_name: Optional[str] = None,
        *,
        force_refresh: bool = False,
    ) -> "AppSecrets":
        settings = get_settings()
        name = secret_name or _resolve_app_secret_name(settings)
        payload = await get_secret_json(name, force_refresh=force_refresh)
        try:
            return cls.model_validate(payload)
        except Exception as exc:
            # Pydantic's ValidationError is verbose and may include values;
            # wrap it so we never echo secret contents into logs/responses.
            raise SecretFormatError(
                f"secret '{name}' is missing or mis-typed required fields "
                "(github_token, repo_url, user_email)"
            ) from exc


def _resolve_app_secret_name(settings: Settings) -> str:
    """Pick the integration-secret name from settings with a sane default."""
    explicit = getattr(settings, "app_secret_name", None)
    if explicit:
        return explicit
    return f"{settings.environment.value}/{settings.app_name}/integrations"


__all__ = [
    "AppSecrets",
    "SecretsManagerClient",
    "SecretsManagerError",
    "SecretNotFoundError",
    "SecretAccessDeniedError",
    "SecretDecryptError",
    "SecretFormatError",
    "get_secret",
    "get_secret_json",
    "get_secret_field",
    "get_secrets_client",
    "invalidate_secret",
    "put_secret",
    "put_secret_json",
]
