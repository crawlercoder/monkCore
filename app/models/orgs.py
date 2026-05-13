"""Pydantic models for the ``orgs`` DynamoDB table.

Organizations own repos and carry a pointer to an AWS Secrets Manager
secret. The secret itself lives in Secrets Manager (see
:mod:`app.services.secrets`); this table only stores the *name*, which
means the secret can be rotated without touching any org record.

Three shapes are modelled:

* :class:`OrgCreate`  — input to create an org (``org_id`` optional).
* :class:`OrgUpdate`  — patch: every field optional.
* :class:`Org`        — the full record as stored/returned.

All three share validation rules defined once on :class:`OrgBase`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, StringConstraints

# Identifiers are URL-safe so they can live in paths/URLs unescaped.
OrgId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_\-]+$",
        strip_whitespace=True,
    ),
]

# Human-readable org name.
OrgName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=200, strip_whitespace=True),
]

# AWS Secrets Manager secret names: 1-512 chars, allowed set
# [A-Za-z0-9/_+=.@-]. See:
# https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_CreateSecret.html
SecretName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=512,
        pattern=r"^[A-Za-z0-9/_+=.@\-]+$",
    ),
]


class OrgBase(BaseModel):
    """Fields shared by create/update/read."""

    model_config = ConfigDict(extra="forbid")

    name: OrgName
    secret_name: SecretName


class OrgCreate(OrgBase):
    """Input payload for :meth:`OrgsRepository.create`.

    ``org_id`` is optional — if omitted, the repository generates a uuid4 hex.
    """

    org_id: Optional[OrgId] = None


class OrgUpdate(BaseModel):
    """Partial patch. ``updated_at`` is always bumped by the repository and
    is deliberately not writable from the outside."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[OrgName] = None
    secret_name: Optional[SecretName] = None


class Org(OrgBase):
    """Full representation as stored in and returned from DynamoDB."""

    org_id: OrgId
    created_at: datetime
    updated_at: datetime


__all__ = ["Org", "OrgBase", "OrgCreate", "OrgId", "OrgName", "OrgUpdate", "SecretName"]
