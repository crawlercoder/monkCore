"""Environment configuration.

Typed, validated, Pydantic-backed settings loaded from environment
variables and a ``.env`` file. Grouped by concern:

* :class:`AWSSettings`              — region, shared endpoint override
* :class:`BedrockSettings`          — model ids, inference params, timeouts
* :class:`DynamoDBSettings`         — every table name in one place
* :class:`SecretsManagerSettings`   — prefix + per-org secret naming
* :class:`EmailSettings`            — SES / SMTP / console delivery
* :class:`Settings`                 — top-level composition + app-wide fields
* **Storage (``base_storage_path`` / ``BASE_STORAGE_PATH``)**  — on-disk
  root for :mod:`app.storage_manager` (default ``/ai-agent`` at the filesystem root)

The top-level ``Settings`` is globally accessible via the cached
:func:`get_settings` singleton. Nested groups are reached through dotted
access: ``get_settings().bedrock.model_id``.

Design notes
------------
Each nested group is its own ``BaseSettings`` subclass with its own
``env_prefix``. This means ``BEDROCK_MODEL_ID`` populates
``settings.bedrock.model_id`` with no bespoke wiring, and each concern's
env vars are self-documenting. Every group reads the same ``.env`` file,
so one file is enough in all environments.

In tests: call ``get_settings.cache_clear()`` after mutating the
environment to pick up new values, or construct a fresh ``Settings()``
explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import re
from enum import Enum
from functools import lru_cache
from typing import Final, Literal, Optional, Self

from pydantic import EmailStr, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_COMMON_SETTINGS = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    case_sensitive=False,
    extra="ignore",
)


def _parse_cors_origins_env(value: object) -> list[str]:
    """Map ``CORS_ORIGINS`` to a list for ``CORSMiddleware``.

    pydantic-settings tries ``json.loads`` for ``List[str]`` fields, which
    breaks on empty values and complicates comma-separated .env lines. The
    env var is therefore stored as a string and converted here.
    """
    if isinstance(value, list):
        out = [str(x) for x in value if str(x).strip()]
        return out or ["*"]
    s = (value or "").strip() if isinstance(value, str) else ""
    if not s:
        return ["*"]
    if s.startswith("["):
        try:
            data = json.loads(s)
            if isinstance(data, list):
                out = [str(x) for x in data if str(x).strip()]
                return out or ["*"]
        except json.JSONDecodeError:
            pass
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return parts or ["*"]


def _optional_aws_service_endpoint(v: object) -> Optional[str]:
    """Coerce bad .env copy/paste (e.g. a whole line that is only a # comment) to None.

    If non-empty, require ``http://`` or ``https://`` so boto3 is never given a
    junk string like the inline comment from a template file.
    """
    if v is None:
        return None
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or s.startswith("#"):
        return None
    if " #" in s:
        s = s.split(" #", 1)[0].strip()
    if not s or s.startswith("#"):
        return None
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return None


def _optional_non_sql_comment_string(v: object) -> Optional[str]:
    """Unset ``DATABASE_URL``-style values that are really template comments."""
    if v is None:
        return None
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or s.startswith("#"):
        return None
    if " #" in s:
        s = s.split(" #", 1)[0].strip()
    if not s or s.startswith("#"):
        return None
    return s


class Environment(str, Enum):
    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class GitlabTokenStorage(str, Enum):
    """Where per-org GitLab PATs are persisted."""

    SECRETS_MANAGER = "secrets_manager"
    LOCAL = "local"  # JSON files under workspace_root — dev only


class PersistBackend(str, Enum):
    """Where jobs/orgs/repos rows are stored."""

    DYNAMODB = "dynamodb"
    MEMORY = "memory"  # in-process — local dev only; no DynamoDB calls


class SecretsBackend(str, Enum):
    """Where generic app secrets (``get_secret*`` / ``put_secret*``) are read from."""

    SECRETS_MANAGER = "secrets_manager"
    INLINE = "inline"  # JSON file + in-process — local dev only; no AWS SM calls


class LogFormat(str, Enum):
    JSON = "json"
    CONSOLE = "console"


class EmailProvider(str, Enum):
    SES = "ses"
    SMTP = "smtp"
    CONSOLE = "console"  # dev: print emails to stdout instead of sending


# --------------------------------------------------------------------------- #
# AWS                                                                         #
# --------------------------------------------------------------------------- #


class AWSSettings(BaseSettings):
    """AWS-wide configuration shared across Bedrock, DynamoDB, SES, etc."""

    model_config = SettingsConfigDict(**_COMMON_SETTINGS, env_prefix="AWS_")

    region: str = "us-east-1"
    # Useful for LocalStack or region-level overrides in CI.
    endpoint_url: Optional[str] = None

    # Bedrock Runtime API key (optional). Same as env ``AWS_BEARER_TOKEN_BEDROCK``;
    # botocore uses it for ``bedrock-runtime`` when set. See
    # https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-use.html
    # Whitespace-only values are treated as unset (empty tokens break auth).
    bearer_token_bedrock: Optional[SecretStr] = None

    @field_validator("endpoint_url", mode="before")
    @classmethod
    def _clean_aws_endpoint_url(cls, v: object) -> object:
        return _optional_aws_service_endpoint(v)

    @field_validator("bearer_token_bedrock", mode="before")
    @classmethod
    def _empty_bearer_token_bedrock(cls, v: object) -> object:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return v


# --------------------------------------------------------------------------- #
# Bedrock                                                                     #
# --------------------------------------------------------------------------- #


class BedrockSettings(BaseSettings):
    """Amazon Bedrock model + inference parameters."""

    model_config = SettingsConfigDict(**_COMMON_SETTINGS, env_prefix="BEDROCK_")

    # Use a geography **inference profile** id (not the bare model id) so
    # ``InvokeModel`` works where on-demand direct ids are blocked — e.g.
    # ``apac.…`` in ap-south-1. Override with ``BEDROCK_MODEL_ID``.
    model_id: str = "apac.anthropic.claude-3-5-sonnet-20241022-v2:0"
    embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    rerank_model_id: Optional[str] = "cohere.rerank-v3-5:0"

    max_tokens: int = Field(default=4096, ge=1, le=200_000)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)

    timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_retries: int = Field(default=5, ge=0, le=20)

    # Optional separate region for Bedrock if it differs from the AWS default
    # (useful when Bedrock model availability forces a specific region).
    region: Optional[str] = None

    @field_validator("region", mode="before")
    @classmethod
    def _bedrock_region_sanitize(cls, v: object) -> object:
        """Drop junk values (e.g. copy-paste from .env where ``BEDROCK_REGION=``
        was followed by ``#`` text parsed as the value).

        Valid AWS region codes look like ``ap-south-1``; everything else
        is treated as unset so :attr:`Settings.bedrock_region` falls back
        to :attr:`AWSSettings.region`.
        """
        if v is None:
            return None
        if not isinstance(v, str):
            return v
        s = v.strip()
        if not s or s.lstrip().startswith("#"):
            return None
        # e.g. "# defaults to AWS …" with no space after `=`
        if s.startswith("#"):
            return None
        # Heuristic: must look like a real partition region id.
        if not re.match(r"^[a-z0-9-]{8,32}$", s):
            return None
        return s


# Bare id that Bedrock rejects in many regions (must use an inference profile).
_LEGACY_BARE_CLAUDE_35_SONNET_V2: Final[str] = (
    "anthropic.claude-3-5-sonnet-20241022-v2:0"
)


def resolve_bedrock_text_model_id_for_region(model_id: str, region: str) -> str:
    """Map the legacy bare Claude 3.5 Sonnet v2 id to a geography inference profile.

    If ``model_id`` is already a profile (``apac.`` / ``us.`` / ``eu.`` prefix) or
    any other value, it is returned unchanged.
    """
    mid = (model_id or "").strip()
    if mid != _LEGACY_BARE_CLAUDE_35_SONNET_V2:
        return mid
    r = (region or "us-east-1").strip().lower()
    if r.startswith("ap-"):
        return "apac.anthropic.claude-3-5-sonnet-20241022-v2:0"
    if r.startswith("us-"):
        return "us.anthropic.claude-3-5-sonnet-20241022-v2:0"
    if r.startswith("eu-"):
        return "eu.anthropic.claude-3-5-sonnet-20241022-v2:0"
    if r.startswith("ca-"):
        return "us.anthropic.claude-3-5-sonnet-20241022-v2:0"
    return "us.anthropic.claude-3-5-sonnet-20241022-v2:0"


# --------------------------------------------------------------------------- #
# DynamoDB                                                                    #
# --------------------------------------------------------------------------- #


class DynamoDBSettings(BaseSettings):
    """Every DynamoDB table name lives here — one source of truth.

    Names are parameterized per-environment via env vars so a single image
    runs everywhere; defaults are safe for local development.
    """

    model_config = SettingsConfigDict(**_COMMON_SETTINGS, env_prefix="DYNAMODB_")

    endpoint_url: Optional[str] = None  # set for local DynamoDB (e.g. http://localhost:8001)

    @field_validator("endpoint_url", mode="before")
    @classmethod
    def _clean_dynamodb_endpoint_url(cls, v: object) -> object:
        return _optional_aws_service_endpoint(v)

    jobs_table: str = "jobs"
    orgs_table: str = "orgs"
    repos_table: str = "repos"
    workflows_table: str = "ai-agent-workflows"
    idempotency_table: str = "ai-agent-idempotency"
    events_outbox_table: str = "ai-agent-events-outbox"
    human_gates_table: str = "ai-agent-human-gates"

    # GSI / secondary index names.
    repos_org_id_index: str = "org_id-index"
    reply_token_index: str = "reply_token-index"


# --------------------------------------------------------------------------- #
# Secrets Manager                                                             #
# --------------------------------------------------------------------------- #


# AWS Secrets Manager name rule: 1-512 chars of [A-Za-z0-9/_+=.@-]
# (https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_CreateSecret.html).
# We re-validate prefix and kind strings against the same class so mistyped
# env values fail fast at startup instead of inside a boto call.
_SECRET_NAME_CHARS = re.compile(r"^[A-Za-z0-9/_+=.@\-]+$")


class SecretsManagerSettings(BaseSettings):
    """AWS Secrets Manager configuration.

    One place to answer two operational questions:

    * **Where** do per-org secrets live? → :attr:`prefix` + :attr:`secret_name_for`
      produce the deterministic path used by every reader and writer.
    * **How long** do we cache secret values in-process? → :attr:`cache_ttl_seconds`
      is read by :class:`~app.services.secrets.SecretsManagerClient`.

    Using a settings-driven prefix makes IAM scoping trivial: a resource
    ARN of ``arn:aws:secretsmanager:<region>:<acct>:secret:<prefix>*``
    covers every secret the app owns, and changing environments
    (``orgs/dev/`` vs ``orgs/prod/``) is a one-line env change.
    """

    model_config = SettingsConfigDict(**_COMMON_SETTINGS, env_prefix="SECRETS_")

    prefix: str = Field(
        default="orgs/",
        description=(
            "Path-style prefix prepended to every per-org secret name. "
            "Trailing slash optional; applied automatically."
        ),
    )
    cache_ttl_seconds: int = Field(default=300, ge=1, le=86_400)

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("SECRETS_PREFIX must not be empty")
        # Normalize: no leading slash, exactly one trailing slash.
        v = v.lstrip("/").rstrip("/") + "/"
        if not _SECRET_NAME_CHARS.match(v):
            raise ValueError(
                f"SECRETS_PREFIX contains invalid chars: {v!r}. "
                "Allowed: A-Z, a-z, 0-9, and any of /_+=.@-"
            )
        return v

    def secret_name_for(
        self, org_id: str, *, kind: str = "gitlab-token"
    ) -> str:
        """Build the canonical Secrets Manager name for an org's secret.

        Example (defaults)::

            >>> settings.secrets_manager.secret_name_for("org_abc")
            'orgs/org_abc/gitlab-token'

        ``kind`` is validated so callers can't accidentally inject a path
        segment (``../../other-org``) that would resolve to another org's
        secret. Unlikely via normal code paths, but cheap to enforce.
        """
        if not org_id:
            raise ValueError("org_id must be a non-empty string")
        if not kind or "/" in kind or not _SECRET_NAME_CHARS.match(kind):
            raise ValueError(f"kind must be a simple name, got {kind!r}")
        return f"{self.prefix}{org_id}/{kind}"


# --------------------------------------------------------------------------- #
# Code-understanding summarization worker                                     #
# --------------------------------------------------------------------------- #


class SummarizationSettings(BaseSettings):
    """Settings for :mod:`app.summary_worker`.

    Exposed to the rest of the app as the logical config blob::

        {
            "enable_summarization": bool,
            "batch_size": int,
            "max_chunks_per_run": int,
        }

    Access via :attr:`Settings.summarization`. Every field can be overridden
    through the ``SUMMARIZATION_`` env prefix (e.g. ``SUMMARIZATION_ENABLED``,
    ``SUMMARIZATION_BATCH_SIZE``, ``SUMMARIZATION_MAX_CHUNKS_PER_RUN``).
    """

    model_config = SettingsConfigDict(**_COMMON_SETTINGS, env_prefix="SUMMARIZATION_")

    enabled: bool = Field(
        default=True,
        description="Master toggle for the per-chunk LLM summarisation pass.",
    )
    batch_size: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Chunks processed between telemetry log lines; recommended 10–20.",
    )
    max_chunks_per_run: int = Field(
        default=100,
        ge=1,
        le=10_000,
        description="Hard upper bound on chunks attempted per invocation.",
    )


# --------------------------------------------------------------------------- #
# Email                                                                       #
# --------------------------------------------------------------------------- #


class EmailSettings(BaseSettings):
    """Outbound email configuration (PM notifications, human-gate emails).

    Three providers:

    * ``ses``      — AWS SES via boto3. Uses the AWS credential chain;
                     region comes from :class:`AWSSettings`.
    * ``smtp``     — Generic SMTP server (e.g. GMail relay, Postfix).
    * ``console``  — Dev mode: print the rendered email to stdout instead of
                     sending. Keeps local workflows fast and offline-safe.

    ``reply_to_domain`` is used to build per-workflow reply-to addresses
    of the form ``agent+<reply_token>@<reply_to_domain>``. The SES inbound
    rule then routes those messages back to the human-gate Lambda.
    """

    model_config = SettingsConfigDict(**_COMMON_SETTINGS, env_prefix="EMAIL_")

    provider: EmailProvider = EmailProvider.CONSOLE
    from_address: Optional[EmailStr] = None
    reply_to_domain: Optional[str] = None

    # SES-specific
    ses_configuration_set: Optional[str] = None

    # SMTP-specific
    smtp_host: Optional[str] = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: bool = True
    smtp_timeout_seconds: int = Field(default=10, ge=1, le=120)

    # Operational
    default_subject_prefix: str = "[ai-agent]"
    send_timeout_seconds: int = Field(default=10, ge=1, le=120)


# --------------------------------------------------------------------------- #
# Top-level                                                                   #
# --------------------------------------------------------------------------- #


class Settings(BaseSettings):
    """Application-wide settings composed from the groups above.

    Fields here are flat because they don't belong to a single AWS service:
    app metadata, HTTP server, logging, CORS, miscellaneous integrations.
    """

    model_config = _COMMON_SETTINGS

    # --- app ---
    app_name: str = "ai-agent-service"
    environment: Environment = Environment.LOCAL
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: str = Field(
        default="*",
        description="Comma-separated origins, a JSON array string, or * (default).",
    )

    # --- logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # --- grouped AWS settings ---
    aws: AWSSettings = Field(default_factory=AWSSettings)
    bedrock: BedrockSettings = Field(default_factory=BedrockSettings)
    dynamodb: DynamoDBSettings = Field(default_factory=DynamoDBSettings)
    secrets_manager: SecretsManagerSettings = Field(
        default_factory=SecretsManagerSettings
    )
    email: EmailSettings = Field(default_factory=EmailSettings)
    summarization: SummarizationSettings = Field(default_factory=SummarizationSettings)

    # --- rag / opensearch ---
    opensearch_endpoint: Optional[str] = None
    opensearch_index: str = "code-chunks"

    # --- eventbridge / workers ---
    event_bus_name: str = "spec2pr-bus"

    # --- agent storage (see :mod:`app.storage_manager`) ---
    # Env: ``BASE_STORAGE_PATH`` (local dev, EC2 disk, or EFS mount path in AWS).
    # Legacy: ``AGENT_SYSTEM_ROOT`` is still read if ``BASE_STORAGE_PATH`` is unset.
    base_storage_path: str = Field(
        default="/ai-agent",
        min_length=1,
        description="Top-level path for repos/, artifacts/, cache/, tmp/ (e.g. /ai-agent on the VM or an EFS mount).",
    )

    # --- local workspace (git / dev files only) ---
    # Local GitLab token files live under ``<workspace_root>/.local-gitlab-tokens``
    # when using ``gitlab_token_storage=local``. Cloned repos use
    # ``base_storage_path``/repos/… (``BASE_STORAGE_PATH``), not this field.
    workspace_root: str = "/var/lib/ai-agent"

    # --- GitLab token persistence (per org) ---
    gitlab_token_storage: GitlabTokenStorage = GitlabTokenStorage.SECRETS_MANAGER

    # --- local dev: skip AWS DynamoDB / Secrets Manager ---
    # PERSIST_BACKEND=memory + ENVIRONMENT=local → jobs/orgs/repos live in RAM.
    persist_backend: PersistBackend = PersistBackend.DYNAMODB
    # ENVIRONMENT=local only: when no AWS credential chain is found (and DynamoDB is
    # not pointed at dynamodb-local), switch to memory persistence + inline secrets +
    # local GitLab token files — see .env.example and README «Local development».
    local_aws_autofallback: bool = True
    # SECRETS_BACKEND=inline + ENVIRONMENT=local → secrets read/written via LOCAL_SECRETS_FILE (or memory-only if unset).
    secrets_backend: SecretsBackend = SecretsBackend.SECRETS_MANAGER
    local_secrets_file: Optional[str] = None

    # --- relational db (optional) ---
    database_url: Optional[str] = None

    @field_validator("database_url", mode="before")
    @classmethod
    def _clean_database_url(cls, v: object) -> object:
        return _optional_non_sql_comment_string(v)

    # --- integrations (app-wide, not per-org) ---
    github_app_id: Optional[str] = None
    github_app_private_key_secret_arn: Optional[str] = None
    app_secret_name: Optional[str] = None

    # --- MR / CI pipeline (optional; :mod:`app.mr_pipeline`) ---
    # GitLab API base for creating merge requests (SaaS: ``/api/v4`` on your host).
    gitlab_api_v4_url: str = "https://gitlab.com/api/v4"
    # Template for the staging line in the MR body; ``{job_id}`` and ``{repo_id}`` are replaced.
    mr_staging_url_template: Optional[str] = None
    # If set, ``POST`` this URL with a JSON body after a successful run (MVP deploy hook).
    mr_deploy_webhook_url: Optional[str] = None

    # --- validators ---

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v):
        return v.upper() if isinstance(v, str) else v

    @field_validator("base_storage_path", mode="before")
    @classmethod
    def _clean_base_storage_path(cls, v: object) -> object:
        if v is None:
            return None
        if not isinstance(v, str):
            return v
        s = v.strip()
        if " #" in s:
            s = s.split(" #", 1)[0].strip()
        if not s or s.startswith("#"):
            return None
        return s

    @model_validator(mode="before")
    @classmethod
    def _legacy_base_storage_path(cls, data: object) -> object:
        """Prefer ``BASE_STORAGE_PATH``; fall back to ``AGENT_SYSTEM_ROOT``."""
        if not isinstance(data, dict):
            return data
        merged = {**data}
        b = merged.get("base_storage_path")
        empty = b is None or (isinstance(b, str) and not b.strip())
        if empty and (legacy := (os.environ.get("AGENT_SYSTEM_ROOT") or "").strip()):
            merged["base_storage_path"] = legacy
        return merged

    def _apply_local_aws_autofallback(self) -> None:
        if self.environment is not Environment.LOCAL or not self.local_aws_autofallback:
            return
        # Lazy import avoids pulling boto3 at module import when only typed settings are needed.
        from app.aws_local import (
            boto_session_has_resolved_credentials,
            skip_local_dynamodb_memory_fallback,
        )

        log = logging.getLogger("app.config")
        has_creds = boto_session_has_resolved_credentials()
        ddb_custom = skip_local_dynamodb_memory_fallback(self)
        if self.aws.endpoint_url:
            log.info(
                "AWS_ENDPOINT_URL is set — using a custom AWS API endpoint "
                "(e.g. LocalStack)."
            )

        changed: list[str] = []

        if not has_creds and self.secrets_backend == SecretsBackend.SECRETS_MANAGER:
            self.secrets_backend = SecretsBackend.INLINE
            changed.append("SECRETS_BACKEND=inline")
        if not has_creds and self.gitlab_token_storage == GitlabTokenStorage.SECRETS_MANAGER:
            self.gitlab_token_storage = GitlabTokenStorage.LOCAL
            changed.append("GITLAB_TOKEN_STORAGE=local")

        if ddb_custom and not has_creds:
            log.info(
                "DYNAMODB_ENDPOINT_URL is set and no boto3 credential chain was found — "
                "persist_backend=left as dynamodb; set dummy AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY "
                "for DynamoDB Local if connects fail."
            )

        if (
            not has_creds
            and self.persist_backend == PersistBackend.DYNAMODB
            and not ddb_custom
        ):
            self.persist_backend = PersistBackend.MEMORY
            changed.append("PERSIST_BACKEND=memory")

        if changed and not has_creds:
            log.warning(
                "LOCAL_AWS_AUTOFALLBACK: no AWS credential chain found — applied: %s. "
                "Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, run `aws configure`, "
                "or use an IAM role for full AWS integration. Bedrock/embeddings still need "
                "IAM (or SSO profile) credentials or AWS_BEARER_TOKEN_BEDROCK.",
                "; ".join(changed),
            )

    @model_validator(mode="after")
    def _local_only_dev_options(self) -> Self:
        self._apply_local_aws_autofallback()
        if self.gitlab_token_storage == GitlabTokenStorage.LOCAL and self.environment is not Environment.LOCAL:
            raise ValueError(
                "gitlab_token_storage=local is only valid when environment=local"
            )
        if self.persist_backend == PersistBackend.MEMORY and self.environment is not Environment.LOCAL:
            raise ValueError("PERSIST_BACKEND=memory requires ENVIRONMENT=local")
        if self.secrets_backend == SecretsBackend.INLINE and self.environment is not Environment.LOCAL:
            raise ValueError("SECRETS_BACKEND=inline requires ENVIRONMENT=local")
        return self

    # --- convenience ---

    @property
    def is_production(self) -> bool:
        return self.environment in (Environment.STAGING, Environment.PROD)

    @property
    def is_local(self) -> bool:
        return self.environment == Environment.LOCAL

    @property
    def cors_allowed_origins(self) -> list[str]:
        return _parse_cors_origins_env(self.cors_origins)

    @property
    def bedrock_region(self) -> str:
        """Bedrock region falls back to the generic AWS region."""
        return self.bedrock.region or self.aws.region


# --------------------------------------------------------------------------- #
# Global access                                                               #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached :class:`Settings` singleton.

    * Import and call anywhere: ``from app.config import get_settings``.
    * Inject into FastAPI routes: ``Depends(get_settings)`` — see
      :mod:`app.api.deps`.
    * In tests: ``get_settings.cache_clear()`` after mutating the env.
    """
    return Settings()


__all__ = [
    "AWSSettings",
    "BedrockSettings",
    "DynamoDBSettings",
    "EmailProvider",
    "EmailSettings",
    "Environment",
    "GitlabTokenStorage",
    "LogFormat",
    "PersistBackend",
    "SecretsBackend",
    "SecretsManagerSettings",
    "Settings",
    "SummarizationSettings",
    "get_settings",
]
