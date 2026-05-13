"""Local development helpers for the AWS credential chain.

``boto3`` resolves credentials from the environment, shared config files,
``AWS_PROFILE``, container / instance roles, etc. This module answers a
single question for :mod:`app.config`: *can we expect SigV4-style AWS calls
to succeed without extra setup?*

When the answer is *no* and ``ENVIRONMENT=local``, settings can fall back to
in-memory persistence and inline secrets so the API still starts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings


def boto_session_has_resolved_credentials() -> bool:
    """Return True if :func:`boto3.Session().get_credentials` yields an access key.

    Cheap and synchronous; suitable for startup. Returns False if the chain
    is empty or boto3 raises (misconfigured SSO profile, broken config, etc.).
    """
    try:
        import boto3

        creds = boto3.Session().get_credentials()
        if creds is None:
            return False
        frozen = creds.get_frozen_credentials()
        ak = getattr(frozen, "access_key", None) or ""
        return bool(str(ak).strip())
    except Exception:
        return False


def skip_local_dynamodb_memory_fallback(settings: Settings) -> bool:
    """True when DynamoDB is configured for a non-regional endpoint.

    Keeps ``persist_backend=dynamodb`` for DynamoDB Local; pair with dummy
    ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` if the chain is empty.
    """
    dyn = getattr(settings.dynamodb, "endpoint_url", None)
    return bool(dyn)


__all__ = [
    "boto_session_has_resolved_credentials",
    "skip_local_dynamodb_memory_fallback",
]
