"""End-to-end merge-request pipeline: spec → plan → per-repo code → tests → flag → git → MR.

Requires each **job** row in Dynamo to include:

* ``org_id``  — which org's repos to touch
* ``spec``    — non-empty product / change spec

:func:`run_job` flips ``status`` to ``running``, then ``succeeded`` (the
application's *completed* state) or ``failed`` on unhandled error.

Optional :class:`app.config.Settings` knobs:

* :attr:`~Settings.gitlab_api_v4_url` — GitLab API base (``/api/v4``).
* :attr:`~Settings.mr_staging_url_template` — template with ``{job_id}`` / ``{repo_id}``.
* :attr:`~Settings.mr_deploy_webhook_url` — POSTed on success to kick off a deploy.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import random
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Final, Optional, TypeVar
from urllib.parse import quote, urlparse

from app.artifact_manager import init_job_artifacts, save_plan, save_summary, write_log
from app.code_generation import generate_repo_changes
from app.config import Settings, get_settings
from app.context_builder import build_context
from app.db.dynamodb import JobNotFoundError, JobsRepository
from app.feature_flag import add_feature_flag
from app.logging import get_logger, log_context, log_event
from app.models.repos import Repo
from app.planning_engine import generate_plan
from app.services.git_clone import _inject_token, _redact_url, _scrub_text
from app.services.gitlab_tokens import GitlabTokensService
from app.services.registry import RegistryService
from app.storage_manager import get_repo_path
from app.test_generator import generate_tests

T = TypeVar("T")
log = get_logger(__name__)

_GIT_TIMEOUT_S = 120.0
_MAX_ATTEMPTS = 3
_BASE_BACKOFF = 1.0
_MR_TITLE_MAX = 120
_MR_DESC_MAX = 20000

_PIPELINE_LOG_FILE = "pipeline.log"


def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _append_job_log(job_id: str, msg: str) -> None:
    """Best-effort append to ``artifacts/{job_id}/logs/pipeline.log``.

    Never raises — artifact IO is a nice-to-have for the HTTP layer and
    the pipeline should continue even if storage is unavailable.
    """
    try:
        write_log(job_id, _PIPELINE_LOG_FILE, f"{_now_ts()} {msg}")
    except Exception as e:  # noqa: BLE001 — truly best-effort
        log.debug("mr_pipeline: append_job_log swallowed: %s", e)


# --------------------------------------------------------------------------- #
# URL / path helpers                                                          #
# --------------------------------------------------------------------------- #


def _to_https_url(repo_url: str) -> str:
    s = (repo_url or "").strip()
    if s.startswith("https://") or s.startswith("http://"):
        return s
    m = re.match(r"^git@([^:]+):(.+)$", s)
    if m:
        path = m.group(2)
        if path.endswith(".git"):
            path = path[:-4]
        return f"https://{m.group(1)}/{path}"
    if s.startswith("ssh://"):
        p = urlparse(s)
        host = p.hostname
        pth = (p.path or "").lstrip("/")
        if not host or not pth:
            raise ValueError(f"unparseable ssh url: {repo_url!r}")
        return f"https://{host}/{pth}"
    raise ValueError(f"unsupported git URL: {repo_url!r}")


def _project_path_for_gitlab_api(repo_url: str) -> str:
    p = urlparse(_to_https_url(repo_url))
    path = (p.path or "").strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path:
        raise ValueError("cannot derive GitLab project path from URL")
    return path


# Similarity floor for "modify" overwrites: anything below this (vs. the
# existing file content) is rejected as suspiciously divergent LLM output.
_MIN_MODIFY_SIMILARITY: Final[float] = 0.4
# Generated content larger than this multiple of the existing file is
# treated as a destructive overwrite.
_MAX_OVERWRITE_GROWTH: Final[float] = 1.5


def _safe_write_under_root(
    root: Path,
    rel_file: str,
    code: str,
    *,
    change_type: str = "",
) -> None:
    """Write ``code`` to ``rel_file`` under ``root`` with safety checks.

    Path-traversal protections, newline handling, and on-disk write are
    unchanged. When the target file already exists we additionally reject:

    * **suspicious large overwrites** — new content > 1.5x the old file size,
    * **low-similarity modifies** — ``change_type == "modify"`` writes whose
      similarity ratio (``difflib.SequenceMatcher``) to the existing file is
      below :data:`_MIN_MODIFY_SIMILARITY`.

    Both checks are skipped when no prior file exists (i.e. file creation).
    """
    rel = (rel_file or "").replace("\\", "/").lstrip("/")
    parts = [x for x in rel.split("/") if x and x != "."]
    if ".." in parts or not rel:
        raise ValueError(f"unsafe file path: {rel_file!r}")
    target = (root / "/".join(parts)).resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise ValueError(f"path escapes repo root: {rel_file!r}")

    new_code = code
    old_code: str | None = None
    if target.is_file():
        try:
            old_code = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            # Binary or unreadable file: write through but skip text-based
            # safety checks so we don't raise on legitimate non-UTF8 assets.
            log.debug(
                "mr_pipeline: pre-write read failed for %s; skipping safety checks: %s",
                rel, e,
            )
            old_code = None

    similarity: float | None = None
    if old_code:
        if len(new_code) > _MAX_OVERWRITE_GROWTH * len(old_code):
            log_event(
                log, "mr_pipeline.write_rejected",
                "suspicious large overwrite",
                level=logging.ERROR,
                rel_path=rel,
                old_size=len(old_code),
                new_size=len(new_code),
                change_type=(change_type or ""),
            )
            raise ValueError("Suspicious large overwrite detected")

        ct = (change_type or "").strip().lower()
        if ct == "modify":
            similarity = difflib.SequenceMatcher(
                a=old_code, b=new_code, autojunk=False,
            ).ratio()
            if similarity < _MIN_MODIFY_SIMILARITY:
                log_event(
                    log, "mr_pipeline.write_rejected",
                    "modify rejected: low similarity",
                    level=logging.ERROR,
                    rel_path=rel,
                    old_size=len(old_code),
                    new_size=len(new_code),
                    similarity=round(similarity, 4),
                    change_type=ct,
                )
                raise ValueError(
                    f"Refusing modify with low similarity for {rel!r}: "
                    f"ratio={similarity:.3f} < {_MIN_MODIFY_SIMILARITY}"
                )

    log_event(
        log, "mr_pipeline.write_file",
        "applying file change",
        rel_path=rel,
        old_size=(len(old_code) if old_code is not None else 0),
        new_size=len(new_code),
        existed=(old_code is not None),
        change_type=(change_type or ""),
        similarity=(round(similarity, 4) if similarity is not None else None),
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_code, encoding="utf-8", newline="\n")


def _apply_changes_list(repo_root: str, files: list[dict[str, str]]) -> int:
    root = Path(repo_root).resolve()
    n = 0
    for item in files:
        p = (item or {}).get("file")
        c = (item or {}).get("code")
        if not p or not isinstance(p, str):
            continue
        s = c if isinstance(c, str) else str(c)
        ct = str((item or {}).get("change_type") or "")
        _safe_write_under_root(root, p, s, change_type=ct)
        n += 1
    return n


# --------------------------------------------------------------------------- #
# Retry helpers                                                               #
# --------------------------------------------------------------------------- #


async def _retry_async(
    name: str,
    factory: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = _MAX_ATTEMPTS,
) -> T:
    """Run ``factory()`` with bounded exponential backoff + jitter."""
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await factory()
        except Exception as e:
            last = e
            if attempt >= max_attempts:
                break
            sleep_s = min(
                _BASE_BACKOFF * (2 ** (attempt - 1)) + random.random() * 0.3,
                30.0,
            )
            log.warning(
                "mr_pipeline: retry %s attempt %s/%s sleep=%.2fs: %s",
                name, attempt, max_attempts, sleep_s, e,
            )
            await asyncio.sleep(sleep_s)
    assert last is not None
    raise last


def _retry_sync(
    name: str,
    fn: Callable[[], T],
    *,
    max_attempts: int = _MAX_ATTEMPTS,
) -> T:
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            last = e
            if attempt >= max_attempts:
                break
            time.sleep(
                min(_BASE_BACKOFF * (2 ** (attempt - 1)) + random.random() * 0.3, 30.0)
            )
            log.warning(
                "mr_pipeline: sync retry %s attempt %s/%s: %s",
                name, attempt, max_attempts, e,
            )
    assert last is not None
    raise last


# --------------------------------------------------------------------------- #
# Git helpers                                                                 #
# --------------------------------------------------------------------------- #


def _git(repo: Path, args: list[str]) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true"}
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=_GIT_TIMEOUT_S,
    )
    if p.returncode != 0:
        err = _scrub_text((p.stderr or p.stdout or "").strip())
        raise RuntimeError(f"git {' '.join(args)} failed: {err}")
    return p.stdout


def _commit_if_dirty(repo: Path, job_id: str, paths: list[str]) -> bool:
    """Stage agent-written paths and commit if the index or tree changed.

    ``git commit -a`` only picks up *tracked* modifications; it does not add
    new untracked files. We ``git add`` each path from the generated plan
    (including new directories/files), then commit.
    """
    for raw in paths:
        p = (raw or "").strip()
        if not p:
            continue
        _git(repo, ["add", "--", p])
    st = _git(repo, ["status", "--porcelain"]).strip()
    if not st:
        return False
    _git(
        repo,
        [
            "-c", "user.name=ai-mr-pipeline",
            "-c", f"user.email=ai-mr+{job_id[:16]}@local.invalid",
            "commit", "-m",
            f"feat: agent update [job {job_id}]",
        ],
    )
    return True


def _git_push(repo_path: Path, branch: str, token: str, repo_url: str) -> str:
    https = _to_https_url(repo_url)
    authed = _inject_token(https, token)
    return _git(
        repo_path,
        ["push", authed, f"HEAD:refs/heads/{branch}"],
    )


def _checkout_new_branch(
    root: Path,
    branch: str,
    target_branch: str,
    *,
    token: Optional[str] = None,
    repo_url: Optional[str] = None,
) -> None:
    """Create/reset ``branch`` from the latest ``target_branch``.

    When ``token`` and ``repo_url`` are provided, fetch through an
    in-memory authed URL (never written to ``.git/config``) so private
    repos work. Falls back to ``HEAD`` if the remote branch can't be
    fetched (useful for isolated tests).
    """
    fetched = False
    if token and repo_url:
        try:
            authed = _inject_token(_to_https_url(repo_url), token)
            _retry_sync(
                "git_fetch",
                lambda: _git(
                    root,
                    [
                        "fetch", "--prune", authed,
                        f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}",
                        "--depth", "1",
                    ],
                ),
            )
            fetched = True
        except Exception as e:
            log.warning(
                "mr_pipeline: authed fetch failed (%s); falling back to origin",
                _scrub_text(str(e)),
            )

    if not fetched:
        try:
            _retry_sync(
                "git_fetch_origin",
                lambda: _git(root, ["fetch", "origin", target_branch, "--depth", "1"]),
            )
            fetched = True
        except Exception as e:
            log.warning(
                "mr_pipeline: origin fetch failed (%s); using HEAD",
                _scrub_text(str(e)),
            )

    try:
        if fetched:
            _git(root, ["checkout", "-B", branch, f"origin/{target_branch}"])
        else:
            _git(root, ["checkout", "-B", branch])
    except Exception:
        log.warning(
            "mr_pipeline: checkout from origin/%s failed; using HEAD",
            target_branch,
        )
        _git(root, ["checkout", "-B", branch])


# --------------------------------------------------------------------------- #
# GitLab MR                                                                   #
# --------------------------------------------------------------------------- #


def _create_merge_request(
    settings: Settings,
    *,
    project_path: str,
    token: str,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
) -> str:
    base = (settings.gitlab_api_v4_url or "").rstrip("/")
    if not base:
        raise RuntimeError("gitlab_api_v4_url is not configured")
    enc = quote(project_path, safe="")
    url = f"{base}/projects/{enc}/merge_requests"
    body = json.dumps(
        {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title[:_MR_TITLE_MAX],
            "description": description[:_MR_DESC_MAX],
            "remove_source_branch": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "PRIVATE-TOKEN": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    return str(data.get("web_url") or data.get("url") or url)


# --------------------------------------------------------------------------- #
# Deploy + staging URL                                                        #
# --------------------------------------------------------------------------- #


def _maybe_deploy(
    settings: Settings,
    *,
    job_id: str,
    org_id: str,
    plan: dict[str, Any],
) -> str | None:
    hook = (settings.mr_deploy_webhook_url or "").strip()
    if not hook:
        log.info("mr_pipeline: deploy webhook not configured; skipping")
        return None
    pl = json.dumps(
        {
            "job_id": job_id,
            "org_id": org_id,
            "event": "deploy_staging",
            "plan_excerpt": json.dumps(plan)[:4000],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        hook,
        data=pl,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    def _one() -> str:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
            return r.geturl() or hook

    try:
        return _retry_sync("deploy_webhook", _one)
    except (urllib.error.URLError, OSError) as e:
        log.error("mr_pipeline: deploy hook failed: %s", _scrub_text(str(e)))
        return None


def _staging_url(settings: Settings, job_id: str, repo_id: str) -> str:
    t = (settings.mr_staging_url_template or "").strip()
    if not t:
        return "*(set MR_STAGING_URL_TEMPLATE for a real staging URL)*"
    try:
        return t.format(job_id=job_id, repo_id=repo_id)
    except Exception:
        return t


# --------------------------------------------------------------------------- #
# Plan helpers                                                                #
# --------------------------------------------------------------------------- #


def _collect_repo_ids(
    plan: dict[str, Any],
    org_repos: list[Repo],
    *,
    job_id: str | None = None,
) -> list[str]:
    """Resolve repo IDs for the pipeline run — **plan-only**, registry-filtered.

    Collects every non-empty ``repo_id`` referenced under ``plan["tasks"]`` and
    ``plan["repos"]``, dedupes, intersects with the org's registry list, sorts
    deterministically, and returns that ordered list. There is **no** fallback to
    READY or any other auto-selection.

    Returns repo IDs that exist in ``org_repos`` and appear in the plan, sorted.
    """
    from_plan: set[str] = set()
    for t in plan.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        r = (t.get("repo_id") or "").strip()
        if r:
            from_plan.add(r)
    for e in plan.get("repos") or []:
        if not isinstance(e, dict):
            continue
        r = (e.get("repo_id") or "").strip()
        if r:
            from_plan.add(r)

    allowed = {r.repo_id for r in org_repos}
    valid_sorted = sorted(from_plan & allowed)
    rejected_sorted = sorted(from_plan - allowed)

    log_event(
        log, "mr_pipeline.repo_resolution",
        "strict repository resolution",
        job_id=job_id,
        repos_from_plan=sorted(from_plan),
        valid_repo_ids=valid_sorted,
        rejected_repo_ids=rejected_sorted,
        n_from_plan=len(from_plan),
        n_valid=len(valid_sorted),
        n_rejected=len(rejected_sorted),
    )
    return valid_sorted


def _branch_name(job_id: str, repo_id: str) -> str:
    a = re.sub(r"[^a-zA-Z0-9._-]+", "-", f"agent-{job_id[:16]}-{repo_id[-20:]}")
    a = (a.strip("-") or "agent-branch")[:200].rstrip("-")
    return a


def _spec_title(spec: str) -> str:
    head = (spec or "").strip().splitlines()[0] if spec else ""
    head = head.strip() or "agent update"
    return f"feat(ai-agent): {head}"[:_MR_TITLE_MAX]


def _repo_tasks(plan: dict[str, Any], repo_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in plan.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        if (t.get("repo_id") or "").strip() == repo_id:
            out.append(t)
    return out


def _build_mr_description(
    *,
    job_id: str,
    repo_id: str,
    spec: str,
    plan: dict[str, Any],
    staging_url: str,
    files_changed: list[str],
) -> str:
    tasks = _repo_tasks(plan, repo_id)
    ff = plan.get("feature_flag") or {}
    lines: list[str] = []
    lines.append(f"## Automated MR for job `{job_id}`")
    lines.append("")
    lines.append("### Spec")
    lines.append("")
    lines.append("> " + (spec.strip().replace("\n", "\n> ") or "_(empty)_"))
    lines.append("")
    if tasks:
        lines.append("### Tasks for this repo")
        for t in tasks:
            desc = str(t.get("description") or "").strip() or "_(no description)_"
            files = t.get("files_to_modify") or []
            files_s = ", ".join(f"`{f}`" for f in files if isinstance(f, str)) or "_(unspecified)_"
            lines.append(f"- **{desc}**  \n  files: {files_s}")
        lines.append("")
    if files_changed:
        lines.append("### Files changed")
        for f in files_changed[:200]:
            lines.append(f"- `{f}`")
        if len(files_changed) > 200:
            lines.append(f"- _(+{len(files_changed) - 200} more)_")
        lines.append("")
    if isinstance(ff, dict) and ff.get("required"):
        flag = str(ff.get("flag_name") or "").strip() or "_(unnamed)_"
        lines.append(f"### Feature flag\n\n- `{flag}` (default **OFF**)")
        lines.append("")
    lines.append("### Staging")
    lines.append("")
    lines.append(f"- {staging_url}")
    lines.append("")
    lines.append("_Generated by the AI merge-request pipeline._")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Per-repo artifact preparation (pure file-writing; no git ops)               #
# --------------------------------------------------------------------------- #


def _prepare_repo_artifacts_sync(
    job_id: str,
    org_id: str,
    spec: str,
    plan: dict[str, Any],
    context: dict[str, Any],
    repo: Repo,
) -> tuple[str, str, str, list[dict[str, str]]]:
    """Generate the per-repo file list (no git ops yet).

    Returns ``(branch, repo_url, target_branch, to_write)`` where
    ``to_write`` is a flat list of ``{"file", "code"}`` items combining
    code changes, tests, and feature-flag wrapping. An empty list means
    "nothing to do for this repo".
    """
    rid = repo.repo_id
    base_br = (repo.branch or "main").strip() or "main"
    with log_context(org_id=org_id, repo_id=rid):
        ch = generate_repo_changes(spec, plan, context, rid)
        raw_list: list[dict[str, Any]] = list((ch or {}).get("changes") or [])
        tests: list[dict[str, str]] = (
            generate_tests(rid, raw_list) if raw_list else []
        )
        # `add_feature_flag` is a no-op for the input when the plan
        # doesn't require a flag, so it's always safe to funnel through.
        final = add_feature_flag(plan, raw_list)

        to_write: list[dict[str, str]] = []
        seen: set[str] = set()
        for c in final:
            if not isinstance(c, dict):
                continue
            f = str(c.get("file") or "").strip()
            if not f or f in seen:
                continue
            seen.add(f)
            to_write.append(
                {
                    "file": f,
                    "code": str(c.get("code", "")),
                    "change_type": str(c.get("change_type") or ""),
                }
            )
        for t in tests:
            if not isinstance(t, dict):
                continue
            f = str(t.get("file") or "").strip()
            if not f or f in seen:
                continue
            seen.add(f)
            to_write.append(
                {
                    "file": f,
                    "code": str(t.get("code", "")),
                    "change_type": str(t.get("change_type") or "create"),
                }
            )

        branch = _branch_name(job_id, rid)
        return branch, repo.repo_url, base_br, to_write


# --------------------------------------------------------------------------- #
# Per-repo git pipeline                                                       #
# --------------------------------------------------------------------------- #


async def _process_one_repo(
    *,
    settings: Settings,
    job_id: str,
    org_id: str,
    spec: str,
    plan: dict[str, Any],
    context: dict[str, Any],
    repo: Repo,
    gitlab_token: str,
) -> dict[str, Any] | None:
    """Generate → apply → branch → commit → push → MR for a single repo.

    Returns a summary dict on success, or ``None`` if there was nothing
    to change (empty generated file list or clean working tree after
    applying changes).
    """
    rid = repo.repo_id
    with log_context(org_id=org_id, repo_id=rid):
        root = Path(get_repo_path(org_id, rid)).resolve()
        if not root.is_dir() or not (root / ".git").is_dir():
            raise RuntimeError(
                f"no git checkout for repo {rid} (expected under {root})"
            )

        branch, repo_url, target_branch, to_write = await asyncio.to_thread(
            _prepare_repo_artifacts_sync, job_id, org_id, spec, plan, context, repo
        )
        if not to_write:
            log_event(
                log, "mr_pipeline.repo_skipped",
                "no file changes generated for repo", repo_id=rid,
            )
            return None

        await asyncio.to_thread(
            _checkout_new_branch,
            root, branch, target_branch,
            token=gitlab_token, repo_url=repo_url,
        )
        n_written = await asyncio.to_thread(_apply_changes_list, str(root), to_write)
        log_event(
            log, "mr_pipeline.files_written",
            "files written to working tree",
            repo_id=rid, count=n_written, branch=branch,
        )

        written_paths = [str((item or {}).get("file") or "") for item in to_write]
        committed = await asyncio.to_thread(
            _commit_if_dirty, root, job_id, written_paths
        )
        if not committed:
            log_event(
                log, "mr_pipeline.repo_clean",
                "no diff after applying generated changes; skipping push",
                repo_id=rid,
            )
            return None

        await _retry_async(
            "git_push",
            lambda: asyncio.to_thread(
                _git_push, root, branch, gitlab_token, repo_url
            ),
        )
        log_event(
            log, "mr_pipeline.pushed",
            "pushed branch to origin",
            repo_id=rid, branch=branch, remote=_redact_url(_to_https_url(repo_url)),
        )

        staging = _staging_url(settings, job_id, rid)
        desc = _build_mr_description(
            job_id=job_id, repo_id=rid, spec=spec, plan=plan,
            staging_url=staging,
            files_changed=[item["file"] for item in to_write],
        )
        title = _spec_title(spec)

        mr_error: str | None = None
        try:
            project_path = _project_path_for_gitlab_api(repo_url)
            mr_url = await _retry_async(
                "create_mr",
                lambda: asyncio.to_thread(
                    _create_merge_request,
                    settings,
                    project_path=project_path,
                    token=gitlab_token,
                    source_branch=branch,
                    target_branch=target_branch,
                    title=title,
                    description=desc,
                ),
            )
        except Exception as e:
            log_event(
                log, "mr_pipeline.mr_failed",
                "failed to open merge request",
                level=40, repo_id=rid, error=_scrub_text(str(e)),
            )
            mr_url = None
            mr_error = _scrub_text(str(e))

        out: dict[str, Any] = {
            "repo_id": rid,
            "branch": branch,
            "target_branch": target_branch,
            "files_changed": len(to_write),
            "mr_url": mr_url,
            "staging_url": staging,
        }
        if mr_error:
            out["mr_error"] = mr_error
        return out


# --------------------------------------------------------------------------- #
# Job status transitions                                                      #
# --------------------------------------------------------------------------- #


_STATUS_RUNNING = "running"
_STATUS_SUCCEEDED = "succeeded"  # the "COMPLETED" state in the job model
_STATUS_FAILED = "failed"
_STATUS_CANCELLED = "cancelled"


class RepositoryProcessingError(Exception):
    """Raised when one or more per-repo pipelines failed.

    Carries the structured per-repo ``errors`` list and any partial
    ``results`` produced before/around the failures so that the failure
    finalizer can persist both into the job summary.
    """

    def __init__(
        self,
        errors: list[dict[str, str]],
        results: list[dict[str, Any]],
    ) -> None:
        self.errors = errors
        self.results = results
        ids = ", ".join(e.get("repo_id", "?") for e in errors) or "(none)"
        super().__init__(
            f"{len(errors)} repository pipeline(s) failed: {ids}"
        )


async def _set_status(jobs: JobsRepository, job_id: str, status: str) -> None:
    try:
        await _retry_async(
            f"update_status:{status}",
            lambda: jobs.update_job(job_id, {"status": status}),
        )
    except Exception as e:
        # Status-write failures are logged but must not mask the primary
        # exception from the pipeline body. A later reconciler can patch
        # abandoned jobs up to a terminal state.
        log.error(
            "mr_pipeline: failed to set status=%s for job=%s: %s",
            status, job_id, _scrub_text(str(e)),
        )


# --------------------------------------------------------------------------- #
# run_job pipeline steps (single-purpose helpers for orchestration)           #
# --------------------------------------------------------------------------- #


async def load_job_step(job_id: str) -> dict[str, Any]:
    """Fetch the job row; raises :class:`JobNotFoundError` if missing.

    Returns the raw DynamoDB item dict (includes ``spec``, ``org_id``, ``status``).
    """
    log_event(
        log, "mr_pipeline.step",
        "load_job start",
        job_id=job_id, step="load_job", phase="start",
    )
    jobs = JobsRepository(get_settings())
    item = await _retry_async("fetch_job", lambda: jobs.get_job(job_id))
    if item is None:
        log_event(
            log, "mr_pipeline.step",
            "load_job failed: not found",
            job_id=job_id, step="load_job", phase="error",
            level=logging.ERROR,
        )
        raise JobNotFoundError(f"job '{job_id}' not found")
    log_event(
        log, "mr_pipeline.step",
        "load_job ok",
        job_id=job_id, step="load_job", phase="end",
    )
    return item


async def initialize_job_step(job_id: str) -> dict[str, Any] | None:
    """Create artifact dirs and re-fetch job (pre-run cancel check).

    Returns the latest job row (or ``None`` if the row vanished), used only to
    detect ``cancelled`` before flipping to ``running``.
    """
    log_event(
        log, "mr_pipeline.step",
        "initialize_job start",
        job_id=job_id, step="initialize_job", phase="start",
    )
    try:
        await asyncio.to_thread(init_job_artifacts, job_id)
    except Exception as e:  # noqa: BLE001 — artifacts are best-effort
        log.warning("mr_pipeline: init_job_artifacts failed: %s", e)

    pre_run = await _retry_async(
        "fetch_job_pre_run",
        lambda: JobsRepository(get_settings()).get_job(job_id),
    )
    log_event(
        log, "mr_pipeline.step",
        "initialize_job end",
        job_id=job_id, step="initialize_job", phase="end",
    )
    return pre_run


async def build_context_step(org_id: str, spec: str) -> tuple[dict[str, Any], int]:
    """RAG / retrieval-only context (no LLM). Returns ``(context_bundle, n_chunks)``."""
    log_event(
        log, "mr_pipeline.step",
        "build_context start",
        org_id=org_id, step="build_context", phase="start",
    )
    context = await asyncio.to_thread(build_context, org_id, spec)
    n_chunks = len((context or {}).get("context") or [])
    log_event(
        log, "mr_pipeline.step",
        "build_context end",
        org_id=org_id, step="build_context", phase="end",
        n_chunks=n_chunks,
    )
    log_event(
        log, "mr_pipeline.context_built",
        "retrieval context built",
        chunks=n_chunks,
    )
    return context, n_chunks


async def generate_plan_step(
    spec: str,
    context: dict[str, Any],
) -> tuple[dict[str, Any], int, int]:
    """LLM planning. Returns ``(plan_dict, n_tasks, n_plan_repos)``.

    Does **not** persist the plan; the caller runs :func:`save_plan` as today.
    """
    log_event(
        log, "mr_pipeline.step",
        "generate_plan start",
        step="generate_plan", phase="start",
    )
    plan = await asyncio.to_thread(generate_plan, spec, context)
    n_tasks = len((plan or {}).get("tasks") or [])
    n_plan_repos = len((plan or {}).get("repos") or [])
    log_event(
        log, "mr_pipeline.step",
        "generate_plan end",
        step="generate_plan", phase="end",
        tasks=n_tasks, plan_repos=n_plan_repos,
    )
    log_event(
        log, "mr_pipeline.plan_ready",
        "plan generated",
        tasks=n_tasks, repos=n_plan_repos,
    )
    return plan, n_tasks, n_plan_repos


async def process_repositories_step(
    job_id: str,
    org_id: str,
    spec: str,
    plan: dict[str, Any],
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Per-repo generate → git → push → MR.

    Returns ``(results, errors)`` where:

    * ``results`` is the list of per-repo summary dicts that completed
      (possibly partial — repos before/after a failure still appear here).
    * ``errors`` is a list of ``{"repo_id", "error"}`` dicts collected for
      every repo whose pipeline raised an :class:`Exception`. The caller
      decides whether to fail the job; this step never silently drops
      failures.
    """
    settings = get_settings()
    registry = RegistryService()
    tokens = GitlabTokensService()

    org_repos = await registry.list_repos_by_org(org_id)
    repo_ids = _collect_repo_ids(plan, org_repos, job_id=job_id)
    if not repo_ids:
        raise ValueError("No repositories selected from plan")

    repo_map = {r.repo_id: r for r in org_repos}

    log_event(
        log, "mr_pipeline.step",
        "process_repositories start",
        job_id=job_id, org_id=org_id,
        step="process_repositories", phase="start",
        selected_repo_ids=repo_ids,
        selected_repo_count=len(repo_ids),
    )

    gitlab_token = await _retry_async(
        "fetch_gitlab_token",
        lambda: tokens.get_gitlab_token_for_org(org_id),
    )

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for rid in repo_ids:
        repo = repo_map[rid]
        try:
            r = await _process_one_repo(
                settings=settings,
                job_id=job_id,
                org_id=org_id,
                spec=spec,
                plan=plan,
                context=context,
                repo=repo,
                gitlab_token=gitlab_token or "",
            )
            if r is not None:
                results.append(r)
                _append_job_log(
                    job_id,
                    f"repo {rid}: files={r['files_changed']} "
                    f"branch={r['branch']} mr_url={r.get('mr_url') or '-'}",
                )
            else:
                _append_job_log(
                    job_id,
                    f"repo {rid}: skipped (no changes)",
                )
        except Exception as e:
            err_msg = _scrub_text(str(e))
            errors.append({"repo_id": rid, "error": err_msg})
            log_event(
                log, "mr_pipeline.repo_failed",
                "per-repo pipeline raised",
                level=logging.ERROR,
                repo_id=rid,
                error=err_msg,
                error_type=type(e).__name__,
                exc_info=True,
            )
            _append_job_log(
                job_id,
                f"repo {rid}: FAILED: {err_msg}",
            )
            # Continue to next repo so we record every failure in this run,
            # but the caller is required to fail the job if `errors` is
            # non-empty (deterministic, observable behavior).
            continue

    total_files_changed = sum(int(r.get("files_changed") or 0) for r in results)
    log_event(
        log, "mr_pipeline.step",
        "process_repositories end",
        job_id=job_id, org_id=org_id,
        step="process_repositories", phase="end",
        result_count=len(results),
        total_files_changed=total_files_changed,
        total_failed_repos=len(errors),
        failed_repo_ids=[e["repo_id"] for e in errors],
    )

    return results, errors


async def finalize_success_step(
    job_id: str,
    results: list[dict[str, Any]],
    *,
    org_id: str,
    plan: dict[str, Any],
    jobs: JobsRepository,
    settings: Settings,
    n_tasks: int,
    n_plan_repos: int,
) -> dict[str, Any]:
    """Deploy hook (best-effort), persist job URLs, write summary JSON, success logs.

    Keyword args carry state from earlier steps so the summary matches the prior
    single-function implementation (same DB writes and artifact layout).
    """
    first_mr = next(
        (r.get("mr_url") for r in results if r.get("mr_url")),
        "",
    )
    first_staging = next(
        (r.get("staging_url") for r in results if r.get("staging_url")),
        "",
    )

    if results and not first_mr and any(r.get("mr_error") for r in results):
        parts = [
            f"{r['repo_id']}: {r['mr_error']}"
            for r in results
            if r.get("mr_error")
        ]
        raise ValueError(
            "merge request could not be created: " + "; ".join(parts)
        )

    log_event(
        log, "mr_pipeline.step",
        "finalize_success start",
        job_id=job_id, org_id=org_id,
        step="finalize_success", phase="start",
        mrs=len(results),
    )

    deploy_url = await asyncio.to_thread(
        _maybe_deploy,
        settings, job_id=job_id, org_id=org_id, plan=plan,
    )

    try:
        await _retry_async(
            "finalize_succeeded",
            lambda: jobs.update_job(
                job_id,
                {
                    "status": _STATUS_SUCCEEDED,
                    "mr_url": first_mr or "",
                    "staging_url": first_staging or "",
                },
            ),
        )
    except Exception as e:
        log.error(
            "mr_pipeline: finalize_succeeded failed: %s",
            _scrub_text(str(e)),
        )

    summary: dict[str, Any] = {
        "job_id": job_id,
        "org_id": org_id,
        "status": _STATUS_SUCCEEDED,
        "mr_url": first_mr or "",
        "staging_url": first_staging or "",
        "plan": {
            "tasks": n_tasks,
            "repos": n_plan_repos,
            "feature_flag": (plan or {}).get("feature_flag") or {},
        },
        "mrs": results,
        "deploy_url": deploy_url,
    }
    try:
        await asyncio.to_thread(save_summary, job_id, summary)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "mr_pipeline: save_summary failed: %s",
            _scrub_text(str(e)),
        )
    log_event(
        log, "mr_pipeline.succeeded",
        "run_job completed",
        mrs=len(results),
        deploy_url=deploy_url,
    )
    _append_job_log(
        job_id,
        f"succeeded mrs={len(results)} mr_url={first_mr or '-'}",
    )
    log_event(
        log, "mr_pipeline.step",
        "finalize_success end",
        job_id=job_id, step="finalize_success", phase="end",
    )
    return summary


async def finalize_failure_step(
    job_id: str,
    error: BaseException,
    *,
    org_id: str,
    jobs: JobsRepository,
    repo_errors: list[dict[str, str]] | None = None,
    results: list[dict[str, Any]] | None = None,
) -> None:
    """Persist failure summary, flip job status to ``failed``, re-raise is left to caller.

    When ``error`` is a :class:`RepositoryProcessingError`, ``repo_errors``
    and partial ``results`` are read off the exception automatically; the
    explicit kwargs let callers force-include them when raising a different
    exception type.
    """
    if isinstance(error, RepositoryProcessingError):
        repo_errors = repo_errors or error.errors
        results = results if results is not None else error.results

    msg = _scrub_text(str(error))
    log_event(
        log, "mr_pipeline.failed",
        "run_job failed",
        level=logging.ERROR,
        error=msg,
        error_type=type(error).__name__,
        total_failed_repos=len(repo_errors or []),
        failed_repo_ids=[e.get("repo_id", "") for e in (repo_errors or [])],
        exc_info=True,
    )
    log_event(
        log, "mr_pipeline.step",
        "finalize_failure",
        job_id=job_id, org_id=org_id,
        step="finalize_failure", phase="error",
        error=msg,
        total_failed_repos=len(repo_errors or []),
    )
    _append_job_log(job_id, f"failed: {msg}")
    if repo_errors:
        for e in repo_errors:
            _append_job_log(
                job_id,
                f"  - {e.get('repo_id', '?')}: {e.get('error', '')}",
            )

    summary: dict[str, Any] = {
        "job_id": job_id,
        "org_id": org_id,
        "status": _STATUS_FAILED,
        "mr_url": "",
        "staging_url": "",
        "error": msg,
    }
    if repo_errors:
        summary["repo_errors"] = repo_errors
    if results:
        summary["mrs"] = results
    try:
        await asyncio.to_thread(save_summary, job_id, summary)
    except Exception as e2:  # noqa: BLE001
        log.warning(
            "mr_pipeline: save_summary (failure) failed: %s",
            _scrub_text(str(e2)),
        )
    await _set_status(jobs, job_id, _STATUS_FAILED)


# --------------------------------------------------------------------------- #
# Public entrypoint                                                           #
# --------------------------------------------------------------------------- #


async def run_job(job_id: str) -> dict[str, Any]:
    """Drive a job end-to-end: spec → plan → per-repo code/tests/flag → MRs.

    Loads the job row first; ``spec`` and ``org_id`` from that row are the
    ones passed to :func:`~app.context_builder.build_context`, to
    :func:`~app.planning_engine.generate_plan`, and to
    :func:`~app.code_generation.generate_repo_changes` (per repo).

    The function is idempotent at the status-field level (callers can
    retry after a transient failure), but it does **not** dedupe Git
    branches — subsequent successful runs push a fresh branch name
    seeded from the job id, so re-running a completed job will open a
    second MR rather than updating the first.
    """
    if not job_id or not isinstance(job_id, str):
        raise ValueError("job_id must be a non-empty string")

    settings = get_settings()
    jobs = JobsRepository(settings)

    with log_context(job_id=job_id):
        # 1. Load job — same semantics as a direct `get_job` + not-found.
        item = await load_job_step(job_id)

        spec = str(item.get("spec") or "").strip()
        org_id = str(item.get("org_id") or "").strip()
        # Same strings as the row: passed into build_context, generate_plan,
        # and generate_repo_changes (see _prepare_repo_artifacts_sync).

        if str(item.get("status") or "").strip().lower() == _STATUS_CANCELLED:
            _append_job_log(job_id, "job already cancelled; skipping run")
            return {
                "job_id": job_id,
                "org_id": org_id,
                "status": _STATUS_CANCELLED,
            }

        # Scaffold artifacts + race check before PROCESSING flips.
        pre_run = await initialize_job_step(job_id)
        if pre_run and str(pre_run.get("status") or "").strip().lower() == _STATUS_CANCELLED:
            _append_job_log(job_id, "cancelled before processing started")
            return {
                "job_id": job_id,
                "org_id": org_id,
                "status": _STATUS_CANCELLED,
            }

        # Flip CREATED → PROCESSING up front so every scheduled run
        # observably passes through this state before it terminates in
        # COMPLETED or FAILED. Even jobs that fail validation follow the
        # PROCESSING → FAILED arc, which matches the public lifecycle the
        # API advertises.
        await _set_status(jobs, job_id, _STATUS_RUNNING)
        log_event(
            log, "mr_pipeline.started",
            "run_job started", org_id=org_id, spec_len=len(spec),
        )
        _append_job_log(job_id, f"started org_id={org_id} spec_len={len(spec)}")

        try:
            # Validate inside the try so failures take the uniform
            # PROCESSING → FAILED path via the except handler below.
            if not spec:
                raise ValueError(f"job '{job_id}' has an empty spec")
            if not org_id:
                raise ValueError(f"job '{job_id}' is missing org_id")

            with log_context(org_id=org_id):
                context, _n_chunks = await build_context_step(org_id, spec)
                _append_job_log(job_id, f"context built: chunks={_n_chunks}")

                plan, n_tasks, n_plan_repos = await generate_plan_step(spec, context)
                try:
                    save_plan(job_id, plan)
                except Exception as e:
                    log.warning(
                        "mr_pipeline: save_plan failed: %s",
                        _scrub_text(str(e)),
                    )
                _append_job_log(
                    job_id,
                    f"plan generated: tasks={n_tasks} repos={n_plan_repos}",
                )

                results, repo_errors = await process_repositories_step(
                    job_id,
                    org_id,
                    spec,
                    plan,
                    context,
                )

                # Any per-repo failure makes the whole job FAILED — silent
                # success on partial completion is not allowed. Errors flow
                # through the standard failure finalizer below.
                if repo_errors:
                    raise RepositoryProcessingError(repo_errors, results)

                return await finalize_success_step(
                    job_id,
                    results,
                    org_id=org_id,
                    plan=plan,
                    jobs=jobs,
                    settings=settings,
                    n_tasks=n_tasks,
                    n_plan_repos=n_plan_repos,
                )

        except asyncio.CancelledError:
            await _set_status(jobs, job_id, _STATUS_CANCELLED)
            _append_job_log(job_id, "cancelled (task stopped)")
            try:
                await asyncio.to_thread(
                    save_summary,
                    job_id,
                    {
                        "job_id": job_id,
                        "org_id": org_id,
                        "status": _STATUS_CANCELLED,
                        "mr_url": "",
                        "staging_url": "",
                        "error": "cancelled",
                    },
                )
            except Exception as e2:  # noqa: BLE001
                log.warning(
                    "mr_pipeline: save_summary (cancelled) failed: %s",
                    _scrub_text(str(e2)),
                )
            log_event(
                log, "mr_pipeline.cancelled",
                "run_job cancelled", org_id=org_id,
            )
            raise

        except Exception as e:
            await finalize_failure_step(job_id, e, org_id=org_id, jobs=jobs)
            raise


def run_job_sync(job_id: str) -> dict[str, Any]:
    """Blocking wrapper around :func:`run_job` for scripts / CLIs.

    Uses :func:`asyncio.run`, so it must not be called from inside an
    already-running event loop.
    """
    return asyncio.run(run_job(job_id))


__all__ = ["run_job", "run_job_sync"]
