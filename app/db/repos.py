"""DynamoDB repository for the ``repos`` table.

Schema::

    Table:  <DYNAMODB_REPOS_TABLE>  (default: "repos")
    PK:     repo_id (S)
    Attrs:  repo_url (S), org_id (S), branch (S), status (S),
            created_at (S), updated_at (S)
    GSI:    <DYNAMODB_REPOS_ORG_ID_INDEX>
            PK: org_id (S)  SK: created_at (S)
            Projection: ALL
    Mode:   PAY_PER_REQUEST

The GSI powers the obvious access pattern: "list every repo for this org,
newest first". Without it, you'd be forced to Scan — which blows up with
fleet size and spend.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import List, Optional, Tuple

from boto3.dynamodb.conditions import Key
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
from app.logging import get_logger, log_event, log_status_transition
from app.models.repos import Repo, RepoCreate, RepoStatus, RepoUpdate

log = get_logger(__name__)

_PARTITION_KEY = "repo_id"


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #


class RepoStatusConflictError(DynamoDBError):
    """The repo's current status doesn't match the expected transition.

    Raised by :meth:`ReposRepository.compare_and_set_status` when another
    worker has already moved the row forward (or backward). Callers should
    treat this as "someone else is handling it" rather than a hard error.
    """


class RepoNotFoundError(DynamoDBError):
    """The requested repo_id does not exist."""


class RepoAlreadyExistsError(DynamoDBError):
    """A put would have clobbered an existing repo."""


# --------------------------------------------------------------------------- #
# Table bootstrap                                                             #
# --------------------------------------------------------------------------- #


async def init_table(
    settings: Optional[Settings] = None,
    *,
    wait: bool = True,
) -> str:
    settings = settings or get_settings()
    table_name = settings.dynamodb.repos_table
    index_name = settings.dynamodb.repos_org_id_index

    if settings.persist_backend == PersistBackend.MEMORY:
        log.info(
            "init_table: persist_backend=memory, skipping DynamoDB repos table (GSI %s)",
            index_name,
        )
        return table_name

    def _create() -> None:
        client = get_resource(settings).meta.client
        try:
            if table_is_present(client, table_name):
                log.info("init_table: '%s' already exists", table_name)
                return
        except (ClientError, BotoCoreError) as exc:
            raise DynamoDBError(f"describe_table failed: {exc}") from exc

        log.info("init_table: creating '%s' with GSI '%s'", table_name, index_name)
        try:
            client.create_table(
                TableName=table_name,
                AttributeDefinitions=[
                    {"AttributeName": _PARTITION_KEY, "AttributeType": "S"},
                    {"AttributeName": "org_id", "AttributeType": "S"},
                    {"AttributeName": "created_at", "AttributeType": "S"},
                ],
                KeySchema=[{"AttributeName": _PARTITION_KEY, "KeyType": "HASH"}],
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": index_name,
                        "KeySchema": [
                            {"AttributeName": "org_id", "KeyType": "HASH"},
                            {"AttributeName": "created_at", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                ],
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
    settings = settings or get_settings()
    table_name = settings.dynamodb.repos_table

    if settings.persist_backend == PersistBackend.MEMORY:
        from app.db.memory_store import ping as mem_ping

        return await mem_ping("repos")

    def _probe() -> Tuple[bool, Optional[str]]:
        client = get_resource(settings).meta.client
        return probe_table_reachable(client, table_name)

    return await asyncio.to_thread(_probe)


# --------------------------------------------------------------------------- #
# Repository                                                                  #
# --------------------------------------------------------------------------- #


class ReposRepository:
    """CRUD + list-by-org on the ``repos`` table."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._table_name = self._settings.dynamodb.repos_table
        self._org_index = self._settings.dynamodb.repos_org_id_index

    @property
    def table_name(self) -> str:
        return self._table_name

    @property
    def _table(self):
        return get_resource(self._settings).Table(self._table_name)

    async def get(self, repo_id: str) -> Optional[Repo]:
        if not repo_id:
            raise ValueError("repo_id must be a non-empty string")

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            return await ms.repos_get(repo_id)

        def _get():
            try:
                resp = self._table.get_item(
                    Key={_PARTITION_KEY: repo_id},
                    ConsistentRead=True,
                )
            except ClientError as exc:
                raise wrap_client_error("get_item", exc) from exc
            return resp.get("Item")

        item = await asyncio.to_thread(_get)
        return Repo.model_validate(item) if item else None

    async def create(self, data: RepoCreate) -> Repo:
        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            item = ms.repo_create_item(data)
            await ms.repos_put(item)
            log_event(
                log, "db.repos.create", "repo row created",
                repo_id=item["repo_id"], org_id=data.org_id,
                status=data.status.value, branch=data.branch,
            )
            return Repo.model_validate(item)

        repo_id = data.repo_id or uuid.uuid4().hex
        now = now_iso()
        # `mode="json"` ensures RepoStatus serialises to its string value
        # ("PENDING") rather than the Enum instance — DynamoDB only speaks
        # strings, numbers, binary, lists, maps, bool, and null.
        item = {
            _PARTITION_KEY: repo_id,
            "repo_url": data.repo_url,
            "org_id": data.org_id,
            "branch": data.branch,
            "status": data.status.value,
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
                    raise RepoAlreadyExistsError(f"repo '{repo_id}' already exists") from exc
                raise wrap_client_error("put_item", exc) from exc

        await asyncio.to_thread(_put)
        log_event(
            log, "db.repos.create", "repo row created",
            repo_id=repo_id, org_id=data.org_id,
            status=data.status.value, branch=data.branch,
        )
        return Repo.model_validate(item)

    async def update(self, repo_id: str, data: RepoUpdate) -> Repo:
        if not repo_id:
            raise ValueError("repo_id must be a non-empty string")

        mutable = data.model_dump(exclude_unset=True, exclude_none=True, mode="json")
        if not mutable:
            raise ValueError("no fields to update")
        mutable["updated_at"] = now_iso()

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            result = await ms.repos_update(repo_id, data)
            log_event(
                log, "db.repos.update", "repo row updated",
                repo_id=repo_id, fields=list(mutable.keys()),
            )
            return result

        expr, names, values = build_update_expression(mutable)
        names["#pk"] = _PARTITION_KEY

        def _update():
            try:
                resp = self._table.update_item(
                    Key={_PARTITION_KEY: repo_id},
                    UpdateExpression=expr,
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                    ConditionExpression="attribute_exists(#pk)",
                    ReturnValues="ALL_NEW",
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise RepoNotFoundError(f"repo '{repo_id}' not found") from exc
                raise wrap_client_error("update_item", exc) from exc
            return resp.get("Attributes", {})

        result = await asyncio.to_thread(_update)
        log_event(
            log, "db.repos.update", "repo row updated",
            repo_id=repo_id, fields=list(mutable.keys()),
        )
        return Repo.model_validate(result)

    async def compare_and_set_status(
        self,
        repo_id: str,
        *,
        expected: RepoStatus,
        new: RepoStatus,
    ) -> Repo:
        """Atomic status transition guarded by a condition expression.

        Writes ``status = new`` only if the current ``status == expected``;
        otherwise raises :class:`RepoStatusConflictError`. The PK existence
        check is folded into the same condition so a missing row surfaces
        as :class:`RepoNotFoundError`.

        This is the primitive the repo-ingest worker uses to claim a row
        (``PENDING → CLONING``) without needing an external lock.
        """
        if not repo_id:
            raise ValueError("repo_id must be a non-empty string")

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            result = await ms.repos_compare_and_set_status(
                repo_id, expected=expected, new=new,
            )
            log_status_transition(
                log, entity="repo",
                from_status=expected.value,
                to_status=new.value,
                repo_id=repo_id,
                trigger="db.compare_and_set_status",
            )
            log_event(
                log, "db.repos.compare_and_set_status", "status transition committed",
                repo_id=repo_id, from_status=expected.value, to_status=new.value,
            )
            return result

        def _update():
            try:
                resp = self._table.update_item(
                    Key={_PARTITION_KEY: repo_id},
                    UpdateExpression="SET #status = :new, #updated_at = :ts",
                    ConditionExpression=(
                        "attribute_exists(#pk) AND #status = :expected"
                    ),
                    ExpressionAttributeNames={
                        "#pk": _PARTITION_KEY,
                        "#status": "status",
                        "#updated_at": "updated_at",
                    },
                    ExpressionAttributeValues={
                        ":new": new.value,
                        ":expected": expected.value,
                        ":ts": now_iso(),
                    },
                    ReturnValues="ALL_NEW",
                )
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == "ConditionalCheckFailedException":
                    # Disambiguate "missing row" vs "status mismatch" with a
                    # single extra read — the two states want different
                    # retry semantics in the caller.
                    existing = self._table.get_item(Key={_PARTITION_KEY: repo_id})
                    if "Item" not in existing:
                        raise RepoNotFoundError(
                            f"repo '{repo_id}' not found"
                        ) from exc
                    current = existing["Item"].get("status")
                    raise RepoStatusConflictError(
                        f"repo '{repo_id}': expected status {expected.value!r}, "
                        f"current is {current!r}"
                    ) from exc
                raise wrap_client_error("update_item", exc) from exc
            return resp.get("Attributes", {})

        result = await asyncio.to_thread(_update)
        # Two log lines by design: one domain-level transition (queryable
        # via ``event=status.transition``) and the DB-level op-log it
        # sits on top of. Dashboards and alerts hang off the transition
        # event; audits and DB-health work happens off ``db.*`` events.
        log_status_transition(
            log, entity="repo",
            from_status=expected.value,
            to_status=new.value,
            repo_id=repo_id,
            trigger="db.compare_and_set_status",
        )
        log_event(
            log, "db.repos.compare_and_set_status", "status transition committed",
            repo_id=repo_id, from_status=expected.value, to_status=new.value,
        )
        return Repo.model_validate(result)

    async def find_by_org_and_url(
        self,
        org_id: str,
        repo_url: str,
        *,
        limit: int = 200,
    ) -> Optional[Repo]:
        """Return the oldest repo for ``org_id`` whose URL matches ``repo_url``.

        Uses the ``org_id-index`` GSI (same access pattern as
        :meth:`list_by_org`) then filters client-side. Matching is
        exact on the raw stored ``repo_url`` — the service layer does
        the canonicalization before calling us so any two URLs that
        refer to the same upstream collapse before they reach DynamoDB.

        Returns ``None`` if no repo matches. Picks the *oldest* match
        when duplicates exist so the caller always sees a stable
        ``repo_id`` (the first-registered wins).
        """
        if not org_id:
            raise ValueError("org_id must be a non-empty string")
        if not repo_url:
            raise ValueError("repo_url must be a non-empty string")
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be in [1, 1000]")

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            return await ms.repos_find_org_url(org_id, repo_url, limit=limit)

        def _query():
            try:
                # ScanIndexForward=True => ascending by SK (created_at),
                # i.e. oldest first. FilterExpression narrows to the
                # target URL. Because the filter runs after read, we
                # may pay for rows we discard; bound with ``Limit`` to
                # keep the worst case predictable.
                resp = self._table.query(
                    IndexName=self._org_index,
                    KeyConditionExpression=Key("org_id").eq(org_id),
                    FilterExpression=Key("repo_url").eq(repo_url),
                    Limit=limit,
                    ScanIndexForward=True,
                )
            except ClientError as exc:
                raise wrap_client_error("query", exc) from exc
            return resp.get("Items", [])

        items = await asyncio.to_thread(_query)
        if not items:
            return None
        return Repo.model_validate(items[0])

    async def list_by_org(
        self,
        org_id: str,
        *,
        limit: int = 50,
        newest_first: bool = True,
    ) -> List[Repo]:
        """Return repos for one org via the ``org_id-index`` GSI.

        GSI reads are eventually consistent — fine for UI lists, not for
        "did my last write land?" checks. Use :meth:`get` for that.
        """
        if not org_id:
            raise ValueError("org_id must be a non-empty string")
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be in [1, 1000]")

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            return await ms.repos_list_org(
                org_id, limit=limit, newest_first=newest_first,
            )

        def _query():
            try:
                resp = self._table.query(
                    IndexName=self._org_index,
                    KeyConditionExpression=Key("org_id").eq(org_id),
                    Limit=limit,
                    ScanIndexForward=not newest_first,
                )
            except ClientError as exc:
                raise wrap_client_error("query", exc) from exc
            return resp.get("Items", [])

        items = await asyncio.to_thread(_query)
        return [Repo.model_validate(i) for i in items]


__all__ = [
    "ReposRepository",
    "RepoAlreadyExistsError",
    "RepoNotFoundError",
    "RepoStatusConflictError",
    "init_table",
    "ping",
]
