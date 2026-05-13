"""Business-logic services: orchestration, composition of agents + rag + db.

Submodules
----------
* :mod:`app.services.secrets`         — low-level AWS Secrets Manager client
  (TTL cache, per-key locks, upsert + read + JSON helpers).
* :mod:`app.services.gitlab_tokens`   — per-org GitLab token storage,
  backed by Secrets Manager and keyed via the orgs table.
* :mod:`app.services.registry`        — high-level façade to manage orgs
  and repos; generates ids, validates relationships, stores tokens.
* :mod:`app.services.readiness`       — probe registry used by ``/ready``.

Name-collision notes
--------------------
``gitlab_tokens.get_secret`` / ``gitlab_tokens.create_secret`` are NOT
re-exported at the package level — their names collide with the raw
``secrets.get_secret``. Import them from the submodule directly::

    from app.services.gitlab_tokens import (
        create_secret, get_secret, get_gitlab_token_for_org,
    )

The classes and typed models (``GitlabSecret``, ``GitlabTokensService``,
``RegistryService``) are re-exported here because they can't collide.
"""

from app.services.git_clone import (
    GitAuthError,
    GitBranchNotFoundError,
    GitCloneConflictError,
    GitCloneError,
    GitCloneService,
    GitCloneTimeoutError,
    GitRepoNotFoundError,
    clone_repo,
)
from app.services.gitlab_tokens import GitlabSecret, GitlabTokensService
from app.services.registry import RegistryService
from app.services.secrets import (
    AppSecrets,
    SecretAccessDeniedError,
    SecretDecryptError,
    SecretFormatError,
    SecretNotFoundError,
    SecretsManagerClient,
    SecretsManagerError,
    get_secret,
    get_secret_field,
    get_secret_json,
    get_secrets_client,
    invalidate_secret,
    put_secret,
    put_secret_json,
)

__all__ = [
    "AppSecrets",
    "GitAuthError",
    "GitBranchNotFoundError",
    "GitCloneConflictError",
    "GitCloneError",
    "GitCloneService",
    "GitCloneTimeoutError",
    "GitRepoNotFoundError",
    "GitlabSecret",
    "GitlabTokensService",
    "RegistryService",
    "SecretAccessDeniedError",
    "SecretDecryptError",
    "SecretFormatError",
    "SecretNotFoundError",
    "SecretsManagerClient",
    "SecretsManagerError",
    "clone_repo",
    "get_secret",
    "get_secret_field",
    "get_secret_json",
    "get_secrets_client",
    "invalidate_secret",
    "put_secret",
    "put_secret_json",
]
