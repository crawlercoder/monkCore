"""DynamoDB connection + 'jobs' table repository.

All connection details come from environment variables via
:class:`app.config.Settings`. Supports local DynamoDB by setting
``DYNAMODB_ENDPOINT_URL``.

Table: ``jobs``

    job_id       S   partition key, uuid4 hex if not supplied
    status       S   CREATED | running | awaiting_human | succeeded | failed | cancelled
                    (``pending`` is accepted for older rows; maps to public CREATED)
    spec         S   the raw product / change spec
    org_id       S   owning org id; immutable after create_job
    mr_url       S   merge-request URL once opened (optional)
    staging_url  S   staging deployment URL once published (optional)
    questions    L   list of maps: { id, text, priority, ... }
    answers      L   list of maps: { question_id, answer, source, confidence }
    created_at   S   ISO-8601 UTC, set once on create
    updated_at   S   ISO-8601 UTC, bumped on every write

The API layer maps internal status strings onto the four public
labels defined in :class:`app.models.jobs.JobStatus`
(``CREATED | PROCESSING | COMPLETED | FAILED``); see
:data:`app.models.jobs.STATUS_DB_TO_API`.

Because FastAPI is async and boto3 is blocking, every I/O call is dispatched
via :func:`asyncio.to_thread` so the event loop is never held.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, Iterable, Optional, Tuple

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.config import PersistBackend, Settings, get_settings
from app.logging import get_logger

log = get_logger(__name__)

DEFAULT_TABLE_NAME = "jobs"
_PARTITION_KEY = "job_id"

_ALLOWED_UPDATE_FIELDS: frozenset[str] = frozenset(
    {"status", "spec", "questions", "answers", "mr_url", "staging_url"}
)


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #


class DynamoDBError(Exception):
    """Base class for all errors raised by this module."""


class JobNotFoundError(DynamoDBError):
    """Raised when the requested job_id does not exist."""


class JobAlreadyExistsError(DynamoDBError):
    """Raised by :meth:`JobsRepository.create_job` on job_id collision."""


# --------------------------------------------------------------------------- #
# Low-level resource factory                                                  #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _resource(
    region: str,
    endpoint_url: Optional[str],
):
    """Cached `boto3` DynamoDB resource.

    Cached by (region, endpoint) so process-wide we hold a single connection
    pool. Credentials come from the standard boto3 chain: env vars
    (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`),
    shared config/credentials files, or the container/instance role.
    """
    cfg = BotoConfig(
        region_name=region,
        retries={"max_attempts": 5, "mode": "adaptive"},
        connect_timeout=3,
        read_timeout=10,
    )
    kwargs: Dict[str, Any] = {"config": cfg}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.resource("dynamodb", **kwargs)


def _get_resource(settings: Settings):
    return _resource(settings.aws.region, settings.dynamodb.endpoint_url)


def _table_exists_by_minimal_scan(client, table_name: str) -> bool:
    """Detect table existence using only data-plane ``Scan`` (``Limit=1``).

    Used when ``describe_table`` returns ``AccessDeniedException`` because
    some roles allow ``dynamodb:Scan`` but not ``dynamodb:DescribeTable``.
    """
    try:
        client.scan(TableName=table_name, Limit=1, Select="COUNT")
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return False
        raise


def table_is_present(client, table_name: str) -> bool:
    """True if *table_name* already exists.

    Prefers ``describe_table``; on ``AccessDeniedException``, falls back to a
    minimal ``Scan`` so narrow IAM policies still work.
    """
    try:
        client.describe_table(TableName=table_name)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            return False
        if code == "AccessDeniedException":
            return _table_exists_by_minimal_scan(client, table_name)
        raise


def probe_table_reachable(client, table_name: str) -> Tuple[bool, Optional[str]]:
    """Readiness probe without ``dynamodb:DescribeTable`` — uses ``Scan``.

    Enough for :func:`ping` when the instance role has item/scan access but
    not control-plane APIs.
    """
    try:
        client.scan(TableName=table_name, Limit=1, Select="COUNT")
        return True, f"table={table_name} ok (scan)"
    except ClientError as exc:
        c = exc.response.get("Error", {}).get("Code", "")
        return False, f"{c or 'ClientError'}: {exc}"
    except BotoCoreError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _wrap_client_error(op: str, exc: ClientError) -> DynamoDBError:
    code = exc.response.get("Error", {}).get("Code", "Unknown")
    msg = exc.response.get("Error", {}).get("Message", str(exc))
    return DynamoDBError(f"{op} failed [{code}]: {msg}")


def _build_update_expression(
    fields: Dict[str, Any],
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """Build a SET UpdateExpression.

    We always route attribute names through ExpressionAttributeNames so that
    DynamoDB reserved words (e.g. ``status``) don't need special-casing by
    callers.
    """
    parts = []
    names: Dict[str, str] = {}
    values: Dict[str, Any] = {}
    for i, (key, value) in enumerate(fields.items()):
        name_placeholder = f"#f{i}"
        value_placeholder = f":v{i}"
        names[name_placeholder] = key
        values[value_placeholder] = value
        parts.append(f"{name_placeholder} = {value_placeholder}")
    return "SET " + ", ".join(parts), names, values


def _filter_update_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop keys callers aren't allowed to write (e.g. job_id, updated_at).

    ``updated_at`` is always managed by the repository.
    """
    cleaned: Dict[str, Any] = {}
    rejected: list[str] = []
    for k, v in data.items():
        if k in _ALLOWED_UPDATE_FIELDS:
            cleaned[k] = v
        else:
            rejected.append(k)
    if rejected:
        log.debug("update_job: ignoring disallowed fields %s", rejected)
    return cleaned


# --------------------------------------------------------------------------- #
# Module-level operations                                                     #
# --------------------------------------------------------------------------- #


async def init_table(
    settings: Optional[Settings] = None,
    *,
    wait: bool = True,
) -> str:
    """Create the ``jobs`` table if it does not already exist. Idempotent.

    Returns the resolved table name.

    In production the table should be provisioned via IaC (Terraform / CDK).
    This helper exists for local dev, integration tests, and first-run bootstrap.
    """
    settings = settings or get_settings()
    table_name = settings.dynamodb.jobs_table or DEFAULT_TABLE_NAME

    if settings.persist_backend == PersistBackend.MEMORY:
        log.info("init_table: persist_backend=memory, skipping DynamoDB jobs table")
        return table_name

    def _create() -> None:
        client = _get_resource(settings).meta.client
        try:
            if table_is_present(client, table_name):
                log.info("init_table: '%s' already exists", table_name)
                return
        except (ClientError, BotoCoreError) as exc:
            raise DynamoDBError(f"describe_table failed: {exc}") from exc

        log.info("init_table: creating '%s'", table_name)
        try:
            # No Tags= here: tagging at create time requires dynamodb:TagResource
            # on the role. Tag tables in IaC or the console if needed.
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
                log.info("init_table: '%s' created concurrently", table_name)
                return
            raise _wrap_client_error("create_table", exc) from exc

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
    table_name = settings.dynamodb.jobs_table or DEFAULT_TABLE_NAME

    if settings.persist_backend == PersistBackend.MEMORY:
        from app.db.memory_store import ping as mem_ping

        return await mem_ping("jobs")

    def _probe() -> Tuple[bool, Optional[str]]:
        client = _get_resource(settings).meta.client
        return probe_table_reachable(client, table_name)

    return await asyncio.to_thread(_probe)


# --------------------------------------------------------------------------- #
# Repository                                                                  #
# --------------------------------------------------------------------------- #


class JobsRepository:
    """CRUD operations on the ``jobs`` table.

    The repository is cheap to construct — the underlying boto3 resource is
    cached at module level — so it's safe to instantiate per-request or keep
    a single instance on the app state. Both patterns work.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._table_name = self._settings.dynamodb.jobs_table or DEFAULT_TABLE_NAME

    @property
    def table_name(self) -> str:
        return self._table_name

    @property
    def _table(self):
        return _get_resource(self._settings).Table(self._table_name)

    async def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return the job with ``job_id`` or ``None`` if it doesn't exist."""
        if not job_id:
            raise ValueError("job_id must be a non-empty string")

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            return await ms.jobs_get(job_id)

        def _get() -> Optional[Dict[str, Any]]:
            try:
                resp = self._table.get_item(
                    Key={_PARTITION_KEY: job_id},
                    ConsistentRead=True,
                )
            except ClientError as exc:
                raise _wrap_client_error("get_item", exc) from exc
            return resp.get("Item")

        return await asyncio.to_thread(_get)

    async def create_job(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Insert a new job. Fills in id / timestamps / defaults.

        Raises :class:`JobAlreadyExistsError` if ``job_id`` is supplied and
        already exists. Returns the item as stored.
        """
        if data is None:
            raise ValueError("data is required")

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            result = await ms.jobs_create_from_dict(dict(data))
            log.info(
                "create_job",
                extra={"job_id": result[_PARTITION_KEY], "status": result["status"]},
            )
            return result

        item = dict(data)
        item.setdefault(_PARTITION_KEY, uuid.uuid4().hex)
        item.setdefault("status", "CREATED")
        item.setdefault("spec", "")
        item.setdefault("questions", [])
        item.setdefault("answers", [])
        # First-class pipeline outputs — defaulted so the API schema
        # (app.models.jobs.Job) always sees a present-but-empty field
        # even before the pipeline has filled them in.
        item.setdefault("mr_url", "")
        item.setdefault("staging_url", "")
        now = _now_iso()
        item.setdefault("created_at", now)
        item["updated_at"] = now

        def _put() -> Dict[str, Any]:
            try:
                self._table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(#pk)",
                    ExpressionAttributeNames={"#pk": _PARTITION_KEY},
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise JobAlreadyExistsError(
                        f"job '{item[_PARTITION_KEY]}' already exists"
                    ) from exc
                raise _wrap_client_error("put_item", exc) from exc
            return item

        result = await asyncio.to_thread(_put)
        log.info(
            "create_job",
            extra={"job_id": result[_PARTITION_KEY], "status": result["status"]},
        )
        return result

    async def update_job(
        self,
        job_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Patch an existing job. ``updated_at`` is always bumped.

        Only the fields in :data:`_ALLOWED_UPDATE_FIELDS` are writable; any
        others in ``data`` are silently ignored to prevent clients from
        clobbering ``job_id`` or ``updated_at``.

        Raises :class:`JobNotFoundError` if the job does not exist.
        """
        if not job_id:
            raise ValueError("job_id must be a non-empty string")
        if not data:
            raise ValueError("data must contain at least one field")

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            result = await ms.jobs_update(job_id, data)
            log.info(
                "update_job",
                extra={"job_id": job_id, "fields": list(_filter_update_fields(data).keys())},
            )
            return result

        mutable = _filter_update_fields(data)
        if not mutable:
            raise ValueError(
                f"no writable fields in data; allowed: {sorted(_ALLOWED_UPDATE_FIELDS)}"
            )
        mutable["updated_at"] = _now_iso()

        expr, names, values = _build_update_expression(mutable)
        names["#pk"] = _PARTITION_KEY

        def _update() -> Dict[str, Any]:
            try:
                resp = self._table.update_item(
                    Key={_PARTITION_KEY: job_id},
                    UpdateExpression=expr,
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                    ConditionExpression="attribute_exists(#pk)",
                    ReturnValues="ALL_NEW",
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise JobNotFoundError(f"job '{job_id}' not found") from exc
                raise _wrap_client_error("update_item", exc) from exc
            return resp.get("Attributes", {})

        result = await asyncio.to_thread(_update)
        log.info(
            "update_job",
            extra={"job_id": job_id, "fields": list(mutable.keys())},
        )
        return result

    async def list_jobs_by_org(
        self,
        org_id: str,
        *,
        limit: int = 100,
    ) -> list[Dict[str, Any]]:
        """Return jobs for ``org_id``, newest ``created_at`` first.

        Implemented as a table :meth:`~boto3.resources.factory.dynamodb.Table.scan`
        with a filter (no GSI on ``org_id``). Suitable for small job tables; use
        a GSI if this becomes hot.
        """
        if not org_id or not str(org_id).strip():
            raise ValueError("org_id must be non-empty")
        limit = max(1, min(int(limit), 1000))

        if self._settings.persist_backend == PersistBackend.MEMORY:
            from app.db import memory_store as ms

            return await ms.jobs_scan_org(org_id, limit=limit)

        def _scan() -> list[Dict[str, Any]]:
            from boto3.dynamodb.conditions import Attr

            out: list[Dict[str, Any]] = []
            kwargs: Dict[str, Any] = {
                "FilterExpression": Attr("org_id").eq(org_id),
            }
            while True:
                try:
                    resp = self._table.scan(**kwargs)
                except ClientError as exc:
                    raise _wrap_client_error("scan", exc) from exc
                out.extend(resp.get("Items", []))
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
            return out

        raw = await asyncio.to_thread(_scan)

        def _sort_key(item: Dict[str, Any]) -> str:
            return str(item.get("created_at") or "")

        raw.sort(key=_sort_key, reverse=True)
        return raw[:limit]


# Public aliases so sibling modules (orgs, repos, …) can share these
# helpers without importing underscore-prefixed names from another file.
get_resource = _get_resource
now_iso = _now_iso
build_update_expression = _build_update_expression
wrap_client_error = _wrap_client_error

__all__: Iterable[str] = (
    "DEFAULT_TABLE_NAME",
    "DynamoDBError",
    "JobAlreadyExistsError",
    "JobNotFoundError",
    "JobsRepository",
    "build_update_expression",
    "get_resource",
    "init_table",
    "now_iso",
    "ping",
    "probe_table_reachable",
    "table_is_present",
    "wrap_client_error",
)
