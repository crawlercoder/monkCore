"""Clone GitLab repositories with token-based HTTPS auth.

Public API
----------
* :func:`clone_repo` — async convenience function matching the requested
  ``(repo_url, token, destination_path)`` signature.
* :class:`GitCloneService` — class form for dependency injection / tests.

Authentication
--------------
GitLab accepts personal, group, project, and deploy tokens as HTTP Basic
credentials with the username ``oauth2`` (or ``gitlab-ci-token`` for job
tokens). We inject them into the URL as::

    https://oauth2:<token>@gitlab.com/group/project.git

Then, **immediately after a successful clone**, we rewrite the stored
``remote.origin.url`` to strip the credentials so the token never
persists in ``.git/config`` on disk. Subsequent fetches performed by
this service re-inject the token per-call.

Redaction
---------
The token is URL-encoded before injection and stripped from every URL
that hits the logger via :func:`_redact_url`. Git's own stderr is
scanned and scrubbed before being included in exception messages, so
downstream log aggregators never see the credential even in failure
traces. ``GIT_ASKPASS=/bin/true`` + ``GIT_TERMINAL_PROMPT=0`` disable
any interactive fallbacks that could leak the URL to a TTY.

Idempotency
-----------
Calling :func:`clone_repo` twice with the same destination is safe:

* first call   — fresh clone (to a ``<dest>.tmp`` sibling, then atomic
  rename to ``<dest>``),
* second call  — detects the existing checkout, verifies its remote
  matches ``repo_url``, fetches the requested branch, and hard-resets
  the working tree to ``origin/<branch>``.

A mismatched origin (someone else's repo sitting at that path) raises
:class:`GitCloneConflictError` instead of silently overwriting data.
Cross-process safety is handled by a ``filelock`` on ``<dest>.lock``.

Retries
-------
Transient failures (network, 5xx, timeout) are retried with
exponential backoff + jitter up to ``max_retries`` times. Non-retryable
failures (auth, not-found, bad branch) surface immediately with a typed
exception so callers can render a useful HTTP error instead of burning
retry budget.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union
from urllib.parse import quote, urlparse, urlunparse

from filelock import FileLock, Timeout as FileLockTimeout

from app.logging import get_logger, log_context, log_event

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #

class GitCloneError(Exception):
    """Base class for git-clone service errors."""


class GitAuthError(GitCloneError):
    """401/403 from the remote — token missing, revoked, or lacks scope."""


class GitRepoNotFoundError(GitCloneError):
    """Remote returned 404 for the repo URL."""


class GitBranchNotFoundError(GitCloneError):
    """The requested branch does not exist on the remote."""


class GitCloneTimeoutError(GitCloneError):
    """git exceeded the configured per-call timeout."""


class GitCloneConflictError(GitCloneError):
    """Destination exists but doesn't match the requested repo."""


# --------------------------------------------------------------------------- #
# URL helpers                                                                 #
# --------------------------------------------------------------------------- #

def _inject_token(url: str, token: str) -> str:
    """Return ``url`` rewritten with ``oauth2:<token>@`` userinfo.

    Rejects non-HTTPS schemes because token auth is only meaningful over
    TLS — ``http://`` would send the token in clear text, ``ssh://`` uses
    keys. URL-encodes the token so characters like ``:`` or ``@`` don't
    produce a malformed authority.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise GitCloneError(
            f"token auth requires https URL, got scheme={parsed.scheme!r}"
        )
    if parsed.scheme == "http":
        # Strictly reject — silent downgrade would leak the token.
        raise GitCloneError("refusing to send token over http (use https)")

    host = parsed.hostname or ""
    if not host:
        raise GitCloneError(f"URL is missing a host: {url!r}")
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    userinfo = f"oauth2:{quote(token, safe='')}"
    return urlunparse(parsed._replace(netloc=f"{userinfo}@{netloc}"))


def _strip_credentials(url: str) -> str:
    """Return ``url`` with any ``user:pass@`` userinfo removed."""
    parsed = urlparse(url)
    if not (parsed.username or parsed.password):
        return url
    host = parsed.hostname or ""
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunparse(parsed._replace(netloc=netloc))


def _redact_url(url: str) -> str:
    """Return ``url`` with userinfo replaced by ``***`` for logging."""
    parsed = urlparse(url)
    if not (parsed.username or parsed.password):
        return url
    host = parsed.hostname or ""
    netloc = f"***@{host}:{parsed.port}" if parsed.port else f"***@{host}"
    return urlunparse(parsed._replace(netloc=netloc))


# Scrubs any `https://user:pass@host` occurrences that git echoed back
# in stderr before we stuff that text into a log line or exception.
_CRED_IN_TEXT = re.compile(r"https?://[^/\s:]+:[^/\s@]+@")


def _scrub_text(text: str) -> str:
    return _CRED_IN_TEXT.sub("https://***@", text)


# --------------------------------------------------------------------------- #
# Error classification                                                        #
# --------------------------------------------------------------------------- #

_AUTH_PATTERNS = (
    "authentication failed",
    "http basic: access denied",
    "401 unauthorized",
    "403 forbidden",
    "access denied",
    "permission denied",
    "invalid_token",
)
_REPO_NOT_FOUND_PATTERNS = (
    "repository not found",
    "not found (404)",
    "404 not found",
    "remote: the project you were looking for could not be found",
)
_BRANCH_NOT_FOUND_PATTERNS = (
    "couldn't find remote ref",
    "remote branch",  # e.g. "Remote branch foo not found in upstream origin"
    "pathspec",       # "pathspec 'X' did not match any file(s) known to git"
)
_TRANSIENT_PATTERNS = (
    "could not resolve host",
    "connection timed out",
    "connection refused",
    "operation timed out",
    "early eof",
    "rpc failed",
    "the requested url returned error: 5",  # 5xx
    "gnutls_handshake() failed",
    "ssl_read: connection was reset",
    "unexpected disconnect",
    "remote end hung up unexpectedly",
)


def _classify_git_error(stderr: str, returncode: int) -> GitCloneError:
    """Map git stderr into a typed exception."""
    lower = stderr.lower()
    msg = _scrub_text(stderr.strip()) or f"git exited with status {returncode}"

    if any(p in lower for p in _AUTH_PATTERNS):
        return GitAuthError(msg)
    if any(p in lower for p in _REPO_NOT_FOUND_PATTERNS):
        return GitRepoNotFoundError(msg)
    if any(p in lower for p in _BRANCH_NOT_FOUND_PATTERNS):
        return GitBranchNotFoundError(msg)
    if any(p in lower for p in _TRANSIENT_PATTERNS):
        # Bare GitCloneError is our "retryable" signal for the retry loop.
        return GitCloneError(msg)
    return GitCloneError(msg)


# Non-retryable types: no amount of backoff fixes them.
_NON_RETRYABLE = (
    GitAuthError,
    GitRepoNotFoundError,
    GitBranchNotFoundError,
    GitCloneConflictError,
)


# --------------------------------------------------------------------------- #
# Subprocess runner                                                           #
# --------------------------------------------------------------------------- #

@dataclass
class _GitResult:
    returncode: int
    stdout: str
    stderr: str


async def _run_git(
    args: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    timeout: float,
) -> _GitResult:
    """Run ``git <args>`` with a hard timeout and no interactive prompts.

    ``GIT_TERMINAL_PROMPT=0`` + ``GIT_ASKPASS=/bin/true`` guarantee that
    any missing credential doesn't hang on stdin — we want a deterministic
    non-zero exit that :func:`_classify_git_error` can turn into
    :class:`GitAuthError`.
    """
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/true",
        "LC_ALL": "C",  # predictable English-language stderr
    }
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        raise GitCloneTimeoutError(
            f"git {args[0] if args else ''} exceeded {timeout:.0f}s"
        ) from exc

    return _GitResult(
        returncode=proc.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #

@dataclass
class GitCloneService:
    """Clone/update a GitLab repo idempotently.

    Default settings are tuned for interactive API calls; for background
    batch workers you may want to raise ``timeout_seconds`` and lower
    ``depth`` to ``None`` for full history.
    """

    timeout_seconds: float = 300.0
    max_retries: int = 3
    base_backoff_seconds: float = 1.5
    default_branch: str = "main"
    default_depth: Optional[int] = 1  # shallow by default; set None for full

    async def clone_repo(
        self,
        repo_url: str,
        token: str,
        destination_path: Union[str, Path],
        *,
        branch: Optional[str] = None,
        depth: Optional[int] = -1,         # sentinel: use default_depth
        force_fresh: bool = False,
    ) -> Path:
        """Clone ``repo_url`` (or update an existing checkout) into ``destination_path``.

        Returns the resolved destination :class:`Path`. Raises a typed
        :class:`GitCloneError` subclass on failure.
        """
        # --- input validation ------------------------------------------------
        if not repo_url or not repo_url.strip():
            raise GitCloneError("repo_url must be non-empty")
        if not token or not token.strip():
            raise GitCloneError("token must be non-empty")
        if not destination_path:
            raise GitCloneError("destination_path must be non-empty")

        repo_url = repo_url.strip()
        branch = (branch or self.default_branch).strip()
        effective_depth = self.default_depth if depth == -1 else depth
        dest = Path(destination_path).expanduser().resolve()
        lock_path = dest.parent / f".{dest.name}.lock"
        dest.parent.mkdir(parents=True, exist_ok=True)

        log_ctx = {
            "repo_url": _redact_url(repo_url),
            "dest": str(dest),
            "branch": branch,
        }

        # Bind the clone context on the enclosing contextvar stack so
        # every log line emitted from ``_run_git`` retry loops, error
        # classifier, and retry helper carries these fields without
        # needing to plumb them through every call.
        with log_context(
            repo_url=_redact_url(repo_url),
            dest=str(dest),
            branch=branch,
        ):
            # --- mutual exclusion across processes ----------------------
            lock = FileLock(str(lock_path), timeout=60)
            try:
                lock.acquire()
            except FileLockTimeout as exc:
                log_event(log, "git.clone.lock_timeout",
                          "another clone already in progress",
                          level=logging.WARNING)
                raise GitCloneError(
                    f"another clone is already in progress at {dest}"
                ) from exc

            log_event(log, "git.clone.start", "clone/update requested")
            try:
                return await self._with_retries(
                    lambda attempt: self._clone_or_update(
                        repo_url=repo_url,
                        token=token,
                        dest=dest,
                        branch=branch,
                        depth=effective_depth,
                        force_fresh=force_fresh,
                        attempt=attempt,
                        log_ctx=log_ctx,
                    ),
                )
            finally:
                lock.release()

    # ================================================================== #
    # Internals                                                          #
    # ================================================================== #

    async def _with_retries(self, op):
        """Exponential backoff with jitter; stops on non-retryable errors."""
        last_exc: Optional[GitCloneError] = None
        for attempt in range(self.max_retries + 1):
            try:
                return await op(attempt)
            except _NON_RETRYABLE:
                raise
            except GitCloneError as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                delay = self.base_backoff_seconds * (2 ** attempt)
                delay += random.uniform(0, delay * 0.25)  # full jitter <= 25%
                log_event(
                    log, "git.clone.retry",
                    f"attempt {attempt + 1}/{self.max_retries + 1} failed, "
                    f"retrying in {delay:.2f}s: {exc}",
                    level=logging.WARNING,
                    attempt=attempt + 1,
                    max_attempts=self.max_retries + 1,
                    delay_seconds=round(delay, 2),
                    error_type=exc.__class__.__name__,
                )
                await asyncio.sleep(delay)
        # Should be unreachable: the loop always returns or raises above.
        assert last_exc is not None
        raise last_exc

    async def _clone_or_update(
        self,
        *,
        repo_url: str,
        token: str,
        dest: Path,
        branch: str,
        depth: Optional[int],
        force_fresh: bool,
        attempt: int,
        log_ctx: dict,
    ) -> Path:
        # Force-fresh: nuke destination and fall through to fresh clone.
        if force_fresh and dest.exists():
            log_event(log, "git.clone.force_fresh",
                      "removing destination for forced fresh clone")
            shutil.rmtree(dest)

        if dest.exists():
            return await self._update_existing(
                repo_url=repo_url, token=token, dest=dest,
                branch=branch, log_ctx=log_ctx,
            )

        log_event(log, "git.clone.fresh_attempt",
                  f"fresh clone attempt {attempt + 1}", attempt=attempt + 1)
        return await self._fresh_clone(
            repo_url=repo_url, token=token, dest=dest,
            branch=branch, depth=depth,
        )

    async def _fresh_clone(
        self,
        *,
        repo_url: str,
        token: str,
        dest: Path,
        branch: str,
        depth: Optional[int],
    ) -> Path:
        """Clone to ``<dest>.tmp`` then atomically rename to ``<dest>``.

        Atomic rename gives us "all or nothing" semantics: a failure
        mid-clone leaves behind only the ``.tmp`` directory (removed on
        the way out), never a half-valid tree at ``dest``.
        """
        tmp = dest.parent / f"{dest.name}.tmp"
        if tmp.exists():
            shutil.rmtree(tmp)

        authed_url = _inject_token(repo_url, token)
        cmd = ["clone", "--single-branch", f"--branch={branch}"]
        if depth:
            cmd.append(f"--depth={depth}")
        cmd += [authed_url, str(tmp)]

        # Pass the credentialed URL as the LAST positional argument so
        # that if argv ever surfaces in logs/ps, the redaction-aware
        # formatter only has to strip one token.
        result = await _run_git(cmd, timeout=self.timeout_seconds)
        if result.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise _classify_git_error(result.stderr, result.returncode)

        # Strip credentials from .git/config so the token doesn't persist.
        scrub = await _run_git(
            ["remote", "set-url", "origin", repo_url],
            cwd=tmp, timeout=30,
        )
        if scrub.returncode != 0:
            # Non-fatal but worth shouting about: delete the checkout
            # rather than ship a config with an embedded token.
            shutil.rmtree(tmp, ignore_errors=True)
            raise GitCloneError(
                f"failed to scrub credentials from remote URL: {_scrub_text(scrub.stderr)}"
            )

        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
        log_event(log, "git.clone.completed",
                  "fresh clone finished",
                  repo_url=_redact_url(repo_url), dest=str(dest))
        return dest

    async def _update_existing(
        self,
        *,
        repo_url: str,
        token: str,
        dest: Path,
        branch: str,
        log_ctx: dict,
    ) -> Path:
        """Fetch + checkout + hard-reset an existing checkout.

        Refuses to touch ``dest`` unless it's a git repo whose
        ``origin`` remote matches ``repo_url`` (credentials stripped on
        both sides). Guards against accidentally overwriting unrelated
        data sitting at the same path.
        """
        if not (dest / ".git").exists():
            raise GitCloneConflictError(
                f"{dest} exists but is not a git repository"
            )

        existing = await _run_git(
            ["config", "--get", "remote.origin.url"],
            cwd=dest, timeout=15,
        )
        if existing.returncode != 0:
            raise GitCloneConflictError(
                f"{dest}: cannot read remote.origin.url "
                f"({_scrub_text(existing.stderr).strip()})"
            )
        existing_url = _strip_credentials(existing.stdout.strip())
        if existing_url != _strip_credentials(repo_url):
            raise GitCloneConflictError(
                f"{dest} is a clone of {existing_url!r}, "
                f"not {_strip_credentials(repo_url)!r}"
            )

        # fetch with credentials injected *only* for this command; the
        # token never touches .git/config.
        authed_url = _inject_token(repo_url, token)
        fetch = await _run_git(
            ["fetch", "--prune", authed_url, f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            cwd=dest, timeout=self.timeout_seconds,
        )
        if fetch.returncode != 0:
            raise _classify_git_error(fetch.stderr, fetch.returncode)

        # Switch to the requested branch (creating a local ref if needed)
        # and hard-reset so the working tree exactly matches origin.
        checkout = await _run_git(
            ["checkout", "-B", branch, f"origin/{branch}"],
            cwd=dest, timeout=60,
        )
        if checkout.returncode != 0:
            raise _classify_git_error(checkout.stderr, checkout.returncode)

        reset = await _run_git(
            ["reset", "--hard", f"origin/{branch}"],
            cwd=dest, timeout=60,
        )
        if reset.returncode != 0:
            raise _classify_git_error(reset.stderr, reset.returncode)

        log_event(log, "git.clone.updated",
                  "existing checkout fetched + reset",
                  repo_url=log_ctx["repo_url"], dest=str(dest))
        return dest


# --------------------------------------------------------------------------- #
# Module-level convenience                                                    #
# --------------------------------------------------------------------------- #

_default_service: Optional[GitCloneService] = None


def _svc() -> GitCloneService:
    global _default_service
    if _default_service is None:
        _default_service = GitCloneService()
    return _default_service


async def clone_repo(
    repo_url: str,
    token: str,
    destination_path: Union[str, Path],
    *,
    branch: Optional[str] = None,
    depth: Optional[int] = -1,
    force_fresh: bool = False,
) -> Path:
    """Clone or update ``repo_url`` into ``destination_path``.

    Thin wrapper over :meth:`GitCloneService.clone_repo`; exists so
    callers who don't need to customize timeouts can use a one-liner.
    """
    return await _svc().clone_repo(
        repo_url, token, destination_path,
        branch=branch, depth=depth, force_fresh=force_fresh,
    )


__all__ = [
    "GitAuthError",
    "GitBranchNotFoundError",
    "GitCloneConflictError",
    "GitCloneError",
    "GitCloneService",
    "GitCloneTimeoutError",
    "GitRepoNotFoundError",
    "clone_repo",
]
