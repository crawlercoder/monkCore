"""DynamoDB repository for the ``orgs`` table.

Schema::

    Table:  <DYNAMODB_ORGS_TABLE>  (default: "orgs")
    PK:     org_id (S)
    Attrs:  name (S), secret_name (S), created_at (S), updated_at (S)
    Mode:   PAY_PER_REQUEST

Shared primitives (boto3 resource, timestamp, update-expression builder,
error wrapping) are imported from :mod:`app.db.dynamodb` so every table
module has identical semantics for retries, logging, and error handling.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import List, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from app.config import PersistBackend, Settings, get_settings
from app.db.dynamodb import (
    DynamoDBError,
    build_update_expression,
    get_resource,
    now_iso,
    probe_table_reachable,
    table_is_present,
    wrap_client_error,
)
from app.logging import get_logger, log_event
from app.models.orgs import Org, OrgCreate, OrgUpdate

log = get_logger(__name__)

_PARTITION_KEY = "org_id"


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #


class OrgNotFoundError(DynamoDBError):
    """The requested org_id does not exist."""


class OrgAlreadyExistsError(DynamoDBError):
    """A put would have clobbered an existing org."""


# --------------------------------------------------------------------------- #
# Table bootstrap                                                             #
# --------------------------------------------------------------------------- #


async def init_table(
    settings: Optional[Settings] = None,
    *,
    wait: bool = True,
) -> str:
    """Create the orgs table if missing. Idempotent. Returns the table name.

    Provisioning should live in IaC in prod; this helper exists for local
    dev, integration tests, and first-run bootstrap.
    """
    settings = settings or get_settings()
    table_name = settings.dynamodb.orgs_table

    if settings.persist_backend == PersistBackend.MEMORY:
        log.info("init_table: persist_backend=memory, skipping DynamoDB orgs table")
        return table_name

    def _create() -> None:
        client = get_resource(settings).meta.client
        try:
            if table_is_present(client, table_name):
                log.info("init_table: '%s' already exists", table_name)
                return
        except (ClientError, BotoCoreError) as exc:
            raise DynamoDBError(f"describe_table failed: {exc}") from exc

        log.info("init_table: creating '%s'", table_name)
        try:
            client.create_table(
                TableName=table_name,
                AttributeDefinitions=[
                    {"AttributeName": _PARTITION_KEY, "AttributeType": "S"},
                ],
                KeySchema=[{"AttributeName": _PARTITION_KEY, "KeyType": "HASH"}],
                BillingMode="PAY_PER_REQUEST",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceInUseException":
                return
            raise wrap_client_error("create_table", exc) from exc

        if wait:
            client.get_waiter("table_exists").wait(
                TableName=table_name,
                WaiterConfig={"Delay": 2, "MaxAttempts": 30},
            )
            log.info("init_table: '%s' is ACTIVE", table_name)

    await asyncio.to_thread(_create)
    return table_name


async def ping(settings: Optional[Settings] = None) -> Tuple[bool, Optional[str]]:
    """Readiness probe: verify the table exists and is ACTIVE."""
    settings = settings or get_settings()
    table_name = settings.dynamodb.orgs_table

    if settings.persist_backend == PersistBackend.MEMORY:
        from app.db.memory_store import ping as mem_ping

        return await mem_ping("orgs")

    def _probe() -> Tuple[bool, Optional[str]]:
        client = get_resource(settings).meta.client
        return probe_table_reachable(client, table_name)

    return await asyncio.to_thread(_probe)


# --------------------------------------------------------------------------- #
# Repository                                                                  #
# --------------------------------------------------------------------------- #


class OrgsRepository:
    """CRUD operations on the ``orgs`` table.

    All inputs/outputs are Pydantic models so validation happens at the
    repository boundary, never deeper in the call stack.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._table_name = self._settings.dynamodb.orgs_table

    @property
    def table_name(self) -> str:
        return self._table_name

    @property
    def _table(self):
        return get_resource(self._settings).Table(self._table_name)

    async def get(self, org_id: str) -> Optional[Org]:
        """Return the org or ``None`` if not found."""
        if not org_id:
            raise ValueError("org_id must be a non-empty string")

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            return await ms.orgs_get(org_id)

        def _get():
            try:
                resp = self._table.get_item(
                    Key={_PARTITION_KEY: org_id},
                    ConsistentRead=True,
                )
            except ClientError as exc:
                raise wrap_client_error("get_item", exc) from exc
            return resp.get("Item")

        item = await asyncio.to_thread(_get)
        return Org.model_validate(item) if item else None

    async def create(self, data: OrgCreate) -> Org:
        """Insert a new org. Raises :class:`OrgAlreadyExistsError` on collision."""
        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            item = ms.org_create_item(data)
            await ms.orgs_put(item)
            log_event(
                log,
                "db.orgs.create",
                "org row created",
                org_id=item["org_id"],
                org_name=data.name,
                secret_name=data.secret_name,
            )
            return Org.model_validate(item)

        org_id = data.org_id or uuid.uuid4().hex
        now = now_iso()
        item = {
            _PARTITION_KEY: org_id,
            "name": data.name,
            "secret_name": data.secret_name,
            "created_at": now,
            "updated_at": now,
        }

        def _put() -> None:
            try:
                self._table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(#pk)",
                    ExpressionAttributeNames={"#pk": _PARTITION_KEY},
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise OrgAlreadyExistsError(f"org '{org_id}' already exists") from exc
                raise wrap_client_error("put_item", exc) from exc

        await asyncio.to_thread(_put)
        log_event(
            log,
            "db.orgs.create",
            "org row created",
            org_id=org_id,
            org_name=data.name,
            secret_name=data.secret_name,
        )
        return Org.model_validate(item)

    async def find_by_name(
        self,
        name: str,
        *,
        limit: int = 500,
    ) -> Optional[Org]:
        """Return the first org whose normalized name matches ``name``.

        "Normalized" means stripped + case-insensitive, so the UI can be
        lenient about whitespace / capitalization when a user re-submits
        the same organization name. Returns ``None`` if no match is
        found.

        Implementation uses a bounded Scan with a ``FilterExpression``.
        This is acceptable because the orgs table is naturally small (a
        handful of rows per deployment). If it ever grows large enough
        to matter, add a GSI keyed by a normalized name attribute and
        switch this method to a Query — the signature doesn't change.

        Note: Scan + client-side filter is best-effort. A concurrent
        ``create`` racing against ``find_by_name`` can still produce two
        rows with the same normalized name. The service layer treats
        ``create_org`` as idempotent (retry-safe) so the caller can
        always collapse duplicates later; in practice uuid-prefixed
        ``org_id`` makes clobbering impossible.
        """
        needle = (name or "").strip().lower()
        if not needle:
            raise ValueError("name must be non-empty")
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be in [1, 1000]")

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            return await ms.orgs_scan_name(name, limit=limit)

        def _scan():
            try:
                resp = self._table.scan(
                    Limit=limit,
                    ProjectionExpression="#pk, #name, secret_name, created_at, updated_at",
                    ExpressionAttributeNames={"#pk": _PARTITION_KEY, "#name": "name"},
                )
            except ClientError as exc:
                raise wrap_client_error("scan", exc) from exc
            return resp.get("Items", [])

        items = await asyncio.to_thread(_scan)
        for item in items:
            item_name = str(item.get("name", "")).strip().lower()
            if item_name == needle:
                return Org.model_validate(item)
        return None

    async def list_all(self, *, limit: int = 100) -> List[Org]:
        """Scan the orgs table, bounded by ``limit``.

        A Scan is fine for the orgs table because the row count is
        naturally small (one per customer) and the UI / debug consumers
        want a single page of results. For anything that could grow
        unbounded, use a GSI-driven Query instead.
        """
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be in [1, 1000]")

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            return await ms.orgs_list(limit=limit)

        def _scan():
            try:
                resp = self._table.scan(Limit=limit)
            except ClientError as exc:
                raise wrap_client_error("scan", exc) from exc
            return resp.get("Items", [])

        items = await asyncio.to_thread(_scan)
        return [Org.model_validate(i) for i in items]

    async def update(self, org_id: str, data: OrgUpdate) -> Org:
        """Patch an org. Only fields set on ``data`` are written.

        ``updated_at`` is always bumped. Raises :class:`OrgNotFoundError`
        if the org doesn't exist.
        """
        if not org_id:
            raise ValueError("org_id must be a non-empty string")

        mutable = data.model_dump(exclude_unset=True, exclude_none=True)
        if not mutable:
            raise ValueError("no fields to update")
        mutable["updated_at"] = now_iso()

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            result = await ms.orgs_update(org_id, data)
            log_event(log, "db.orgs.update", "org row updated",
                      org_id=org_id, fields=list(mutable.keys()))
            return result

        expr, names, values = build_update_expression(mutable)
        names["#pk"] = _PARTITION_KEY

        def _update():
            try:
                resp = self._table.update_item(
                    Key={_PARTITION_KEY: org_id},
                    UpdateExpression=expr,
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                    ConditionExpression="attribute_exists(#pk)",
                    ReturnValues="ALL_NEW",
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise OrgNotFoundError(f"org '{org_id}' not found") from exc
                raise wrap_client_error("update_item", exc) from exc
            return resp.get("Attributes", {})

        result = await asyncio.to_thread(_update)
        log_event(log, "db.orgs.update", "org row updated",
                  org_id=org_id, fields=list(mutable.keys()))
        return Org.model_validate(result)


__all__ = [
    "OrgsRepository",
    "OrgNotFoundError",
    "OrgAlreadyExistsError",
    "init_table",
    "ping",
]
