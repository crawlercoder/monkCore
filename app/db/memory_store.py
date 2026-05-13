"""In-process persistence for ``PERSIST_BACKEND=memory`` (local dev only).

Stores jobs, orgs, and repos in module-level dicts guarded by an asyncio lock.
Not durable across process restarts; avoids DynamoDB entirely.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.db.dynamodb import (
    JobAlreadyExistsError,
    JobNotFoundError,
    _ALLOWED_UPDATE_FIELDS,
    now_iso,
)
from app.db.orgs import OrgAlreadyExistsError, OrgNotFoundError
from app.db.repos import (
    RepoAlreadyExistsError,
    RepoNotFoundError,
    RepoStatusConflictError,
)
from app.models.orgs import Org, OrgCreate, OrgUpdate
from app.models.repos import Repo, RepoCreate, RepoStatus, RepoUpdate

_lock = asyncio.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}
_orgs: Dict[str, Dict[str, Any]] = {}
_repos: Dict[str, Dict[str, Any]] = {}

_JOB_PK = "job_id"
_ORG_PK = "org_id"
_REPO_PK = "repo_id"


def _jc(item: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(item)


def _filter_job_updates(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in data.items():
        if k in _ALLOWED_UPDATE_FIELDS:
            out[k] = v
    return out


async def ping(_table_label: str) -> Tuple[bool, Optional[str]]:
    return True, f"in-memory ({_table_label})"


async def jobs_get(job_id: str) -> Optional[Dict[str, Any]]:
    async with _lock:
        item = _jobs.get(job_id)
        return _jc(item) if item else None


async def jobs_put(item: Dict[str, Any]) -> Dict[str, Any]:
    jid = item[_JOB_PK]
    async with _lock:
        if jid in _jobs:
            raise JobAlreadyExistsError(f"job '{jid}' already exists")
        _jobs[jid] = _jc(item)
        return _jc(_jobs[jid])


async def jobs_update(job_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    async with _lock:
        if job_id not in _jobs:
            raise JobNotFoundError(f"job '{job_id}' not found")
        mutable = _filter_job_updates(data)
        if not mutable:
            raise ValueError(
                f"no writable fields in data; allowed: {sorted(_ALLOWED_UPDATE_FIELDS)}"
            )
        mutable = dict(mutable)
        mutable["updated_at"] = now_iso()
        row = _jobs[job_id]
        for k, v in mutable.items():
            row[k] = v
        return _jc(row)


async def jobs_scan_org(org_id: str, *, limit: int) -> List[Dict[str, Any]]:
    async with _lock:
        matched = [
            _jc(it)
            for it in _jobs.values()
            if str(it.get("org_id") or "") == org_id
        ]

    def _sort_key(it: Dict[str, Any]) -> str:
        return str(it.get("created_at") or "")

    matched.sort(key=_sort_key, reverse=True)
    return matched[:limit]


async def orgs_get(org_id: str) -> Optional[Org]:
    async with _lock:
        item = _orgs.get(org_id)
        return Org.model_validate(_jc(item)) if item else None


async def orgs_put(item: Dict[str, Any]) -> None:
    oid = item[_ORG_PK]
    async with _lock:
        if oid in _orgs:
            raise OrgAlreadyExistsError(f"org '{oid}' already exists")
        _orgs[oid] = _jc(item)


async def orgs_scan_name(name: str, *, limit: int) -> Optional[Org]:
    _ = limit  # Dynamo uses Scan(Limit=…); org count is tiny so we scan all rows.
    needle = (name or "").strip().lower()
    async with _lock:
        for raw in _orgs.values():
            if str(raw.get("name", "")).strip().lower() == needle:
                return Org.model_validate(_jc(raw))
    return None


async def orgs_list(*, limit: int) -> List[Org]:
    async with _lock:
        scanned = list(_orgs.values())[:limit]
    return [Org.model_validate(_jc(raw)) for raw in scanned]


async def orgs_update(org_id: str, data: OrgUpdate) -> Org:
    mutable = data.model_dump(exclude_unset=True, exclude_none=True)
    if not mutable:
        raise ValueError("no fields to update")
    mutable["updated_at"] = now_iso()
    async with _lock:
        if org_id not in _orgs:
            raise OrgNotFoundError(f"org '{org_id}' not found")
        row = _orgs[org_id]
        for k, v in mutable.items():
            row[k] = v
        return Org.model_validate(_jc(row))


async def repos_get(repo_id: str) -> Optional[Repo]:
    async with _lock:
        item = _repos.get(repo_id)
        return Repo.model_validate(_jc(item)) if item else None


async def repos_put(item: Dict[str, Any]) -> None:
    rid = item[_REPO_PK]
    async with _lock:
        if rid in _repos:
            raise RepoAlreadyExistsError(f"repo '{rid}' already exists")
        _repos[rid] = _jc(item)


async def repos_update(repo_id: str, data: RepoUpdate) -> Repo:
    mutable = data.model_dump(exclude_unset=True, exclude_none=True, mode="json")
    if not mutable:
        raise ValueError("no fields to update")
    mutable["updated_at"] = now_iso()
    async with _lock:
        if repo_id not in _repos:
            raise RepoNotFoundError(f"repo '{repo_id}' not found")
        row = _repos[repo_id]
        for k, v in mutable.items():
            row[k] = v
        return Repo.model_validate(_jc(row))


async def repos_compare_and_set_status(
    repo_id: str,
    *,
    expected: RepoStatus,
    new: RepoStatus,
) -> Repo:
    async with _lock:
        if repo_id not in _repos:
            raise RepoNotFoundError(f"repo '{repo_id}' not found")
        row = _repos[repo_id]
        current = row.get("status")
        if current != expected.value:
            raise RepoStatusConflictError(
                f"repo '{repo_id}': expected status {expected.value!r}, "
                f"current is {current!r}"
            )
        row["status"] = new.value
        row["updated_at"] = now_iso()
        return Repo.model_validate(_jc(row))


async def repos_find_org_url(org_id: str, repo_url: str, *, limit: int) -> Optional[Repo]:
    async with _lock:
        candidates = [
            _jc(r)
            for r in _repos.values()
            if r.get("org_id") == org_id and r.get("repo_url") == repo_url
        ]

    def _key(r: Dict[str, Any]) -> str:
        return str(r.get("created_at") or "")

    candidates.sort(key=_key)
    if not candidates:
        return None
    return Repo.model_validate(candidates[0])


async def repos_list_org(
    org_id: str,
    *,
    limit: int,
    newest_first: bool,
) -> List[Repo]:
    async with _lock:
        items = [
            _jc(r)
            for r in _repos.values()
            if r.get("org_id") == org_id
        ]

    def _key(r: Dict[str, Any]) -> str:
        return str(r.get("created_at") or "")

    items.sort(key=_key, reverse=newest_first)
    return [Repo.model_validate(r) for r in items[:limit]]


async def jobs_create_from_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(data)
    item.setdefault(_JOB_PK, uuid.uuid4().hex)
    item.setdefault("status", "CREATED")
    item.setdefault("spec", "")
    item.setdefault("questions", [])
    item.setdefault("answers", [])
    item.setdefault("mr_url", "")
    item.setdefault("staging_url", "")
    ts = now_iso()
    item.setdefault("created_at", ts)
    item["updated_at"] = ts
    return await jobs_put(item)


def org_create_item(data: OrgCreate) -> Dict[str, Any]:
    org_id = data.org_id or uuid.uuid4().hex
    ts = now_iso()
    return {
        _ORG_PK: org_id,
        "name": data.name,
        "secret_name": data.secret_name,
        "created_at": ts,
        "updated_at": ts,
    }


def repo_create_item(data: RepoCreate) -> Dict[str, Any]:
    repo_id = data.repo_id or uuid.uuid4().hex
    ts = now_iso()
    return {
        _REPO_PK: repo_id,
        "repo_url": data.repo_url,
        "org_id": data.org_id,
        "branch": data.branch,
        "status": data.status.value,
        "created_at": ts,
        "updated_at": ts,
    }
