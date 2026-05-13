"""Shared :mod:`boto3` client for ``bedrock-runtime``.

`IAM SigV4`_ is the default credential chain. For `Bedrock API keys`_, set
``AWS_BEARER_TOKEN_BEDROCK`` in the environment (or
:class:`~app.config.AWSSettings.bearer_token_bedrock` in ``.env``) — the SDK
picks it up for ``InvokeModel`` on ``bedrock-runtime``.

.. _IAM SigV4: https://docs.aws.amazon.com/bedrock/latest/userguide/security_iam_id-based-policy-examples.html
.. _Bedrock API keys: https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-use.html
"""

from __future__ import annotations

import os
import threading
from typing import Any

import boto3
from botocore.config import Config

from app.config import get_settings

_client_lock = threading.Lock()
_client: Any = None


def _bedrock_botocore_config() -> Config:
    s = get_settings()
    b = s.bedrock
    return Config(
        connect_timeout=10,
        read_timeout=b.timeout_seconds,
        max_pool_connections=20,
        retries={"max_attempts": max(1, b.max_retries), "mode": "adaptive"},
    )


def _sync_bearer_token_to_environ() -> None:
    """Sync :attr:`app.config.AWSSettings.bearer_token_bedrock` to ``os.environ``.

    Botocore reads ``AWS_BEARER_TOKEN_BEDROCK`` for Bedrock API key auth. An
    empty/whitespace value is **removed** so the SDK can fall back to IAM
    instead of sending a blank bearer string (`botocore#3603`).
    """
    s = get_settings()
    tok = s.aws.bearer_token_bedrock
    if tok is not None:
        raw = tok.get_secret_value().strip()
        if raw:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = raw
        else:
            os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
    else:
        v = (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()
        if not v and "AWS_BEARER_TOKEN_BEDROCK" in os.environ:
            os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)


def get_bedrock_runtime() -> Any:
    """Lazy, process-wide ``bedrock-runtime`` client (thread-safe)."""
    global _client
    with _client_lock:
        if _client is None:
            _sync_bearer_token_to_environ()
            s = get_settings()
            kwargs: dict[str, Any] = {
                "service_name": "bedrock-runtime",
                "region_name": s.bedrock_region,
                "config": _bedrock_botocore_config(),
            }
            if s.aws.endpoint_url:
                kwargs["endpoint_url"] = s.aws.endpoint_url
            _client = boto3.client(**kwargs)
        return _client


def reset_bedrock_runtime_client() -> None:
    """Drop the cached client (for tests after :func:`get_settings.cache_clear`).

    Next :func:`get_bedrock_runtime` call builds a new client and re-reads
    ``AWS_BEARER_TOKEN_BEDROCK`` / settings.
    """
    global _client
    with _client_lock:
        _client = None


__all__ = ["get_bedrock_runtime", "reset_bedrock_runtime_client"]
