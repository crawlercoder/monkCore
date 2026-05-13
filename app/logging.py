"""Structured logging setup.

Two formats are supported, chosen by ``Settings.log_format``:

* ``json``    — one JSON object per line, production-friendly, easy to
                ship to CloudWatch / Datadog / Loki.
* ``console`` — compact, greppable single-line format for local dev.

Correlation context
-------------------
Every log record automatically carries four correlation fields, set via
:class:`contextvars.ContextVar` so they propagate across ``await`` points
and :func:`asyncio.to_thread` calls without manual plumbing:

* ``request_id`` — per-HTTP-request (set by :mod:`app.middleware`).
* ``job_id``     — top-level AI-agent job id (set by job runner).
* ``org_id``     — owning organization (bound once known).
* ``repo_id``    — repo being processed (bound once known).

Plus an arbitrary ``extra`` bag of fields bound via :func:`log_context`
for things like ``branch``, ``secret_name``, ``dest``, ``attempt``.

Context is always rendered with a ``-`` placeholder when unset so log
lines stay schema-stable — grep / JSON filters never have to deal with
missing keys.

Tracing a job
-------------
To follow a single job end-to-end::

    # ship JSON logs (the default via LOG_FORMAT=json) then:
    jq 'select(.repo_id == "repo_abc123")' logs.jsonl
    jq 'select(.request_id == "9f4c…")   ' logs.jsonl
    jq 'select(.event | startswith("repo_ingest"))' logs.jsonl

Conventions for emitters
------------------------
* Use :func:`log_event` instead of free-form strings when you want the
  record to be queryable — it enforces an ``event`` key.
* Use :func:`log_status_transition` for any state-machine transition;
  it emits a standard ``event=status.transition`` shape with
  ``entity`` / ``from_status`` / ``to_status``.
* Never pass raw secrets / tokens / credentialed URLs through ``extra``
  — this module won't redact them for you.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
import time
from typing import Any, Dict, Iterator, Mapping, Optional

from app.config import LogFormat, Settings

# --------------------------------------------------------------------------- #
# Correlation context                                                         #
# --------------------------------------------------------------------------- #

# Default "-" keeps every log line schema-stable: a grep for `org_id=-`
# reliably finds "no org bound" rather than "field missing".
_DEFAULT = "-"

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=_DEFAULT
)
job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "job_id", default=_DEFAULT
)
org_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "org_id", default=_DEFAULT
)
repo_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "repo_id", default=_DEFAULT
)

# Ad-hoc extras bound by :func:`log_context` — think branch, dest,
# attempt, secret_name, error_type, etc. Immutable-ish: each bind
# replaces the mapping rather than mutating it, so nested binds stack
# predictably and exits cleanly via a token reset.
_extra_context_var: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "log_extra_context", default={}
)

# First-class correlation fields — always emitted, in this order, so
# humans reading console logs see them in a predictable slot.
_CORRELATION_FIELDS = ("request_id", "job_id", "org_id", "repo_id")
_CORRELATION_VARS = {
    "request_id": request_id_var,
    "job_id": job_id_var,
    "org_id": org_id_var,
    "repo_id": repo_id_var,
}


# --------------------------------------------------------------------------- #
# log_context: bind correlation fields for a block                            #
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind log fields for the duration of a ``with`` block.

    Works identically in sync and async code because the underlying
    :class:`contextvars.ContextVar` values propagate across ``await``
    points and copy into threads started by :func:`asyncio.to_thread`.

    First-class keys (``request_id``, ``job_id``, ``org_id``, ``repo_id``)
    override the dedicated ContextVars; anything else is merged into the
    ad-hoc extras bag. Passing ``None`` for a field clears it to the
    default (``"-"``) for the duration of the block.

    Example::

        async with log_context_async(...) not needed — ``with`` works fine:
        with log_context(repo_id="repo_x", branch="main"):
            await do_work()  # every log from `do_work` includes those
    """
    tokens: list = []
    try:
        for key in _CORRELATION_FIELDS:
            if key in fields:
                value = fields.pop(key)
                tokens.append(
                    (_CORRELATION_VARS[key], _CORRELATION_VARS[key].set(
                        _DEFAULT if value is None else str(value)
                    ))
                )

        if fields:
            current = dict(_extra_context_var.get())
            current.update(fields)
            tokens.append((_extra_context_var, _extra_context_var.set(current)))

        yield
    finally:
        # Reset in reverse order so nested binds unwind correctly.
        for var, tok in reversed(tokens):
            var.reset(tok)


# --------------------------------------------------------------------------- #
# Filter + formatters                                                         #
# --------------------------------------------------------------------------- #

# Built-in LogRecord attribute names — we mustn't emit these as user
# fields or json.dumps will double-serialize things like record.args.
_RESERVED_LOGRECORD_KEYS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


def _safe_log_extra(d: Dict[str, Any]) -> Dict[str, Any]:
    """Rename keys that :meth:`logging.Logger.makeRecord` rejects (must not
    collide with :class:`LogRecord` attributes, including ``name`` and
    ``message`` — see CPython ``makeRecord``).
    """
    out: Dict[str, Any] = {}
    for k, v in d.items():
        if k in _RESERVED_LOGRECORD_KEYS:
            out[f"ctx_{k}"] = v
        else:
            out[k] = v
    return out


class _ContextFilter(logging.Filter):
    """Copy correlation context and extras onto every LogRecord.

    Runs before the formatter. Fields that were passed explicitly via
    ``logger.info(..., extra={...})`` take precedence over ContextVar
    values — the caller knows better than the context.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _CORRELATION_VARS.items():
            if not hasattr(record, name):
                setattr(record, name, var.get())

        extras = _extra_context_var.get()
        if extras:
            for key, value in extras.items():
                if key in _RESERVED_LOGRECORD_KEYS:
                    continue
                if not hasattr(record, key):
                    setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """Minimal JSON formatter with no external deps.

    Emits one object per line with a stable top-level shape::

        {
          "ts": "...Z",
          "level": "INFO",
          "logger": "app.workers.repo_ingest",
          "message": "...",
          "event": "repo_ingest.finished",   # optional
          "request_id": "...", "job_id": "...", "org_id": "...", "repo_id": "...",
          "<any extra fields>": ...
        }
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": _iso_ts(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Correlation fields are always present (default "-") so consumers
        # can filter on them without null-checks.
        for name in _CORRELATION_FIELDS:
            payload[name] = getattr(record, name, _DEFAULT)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info

        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_KEYS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = _json_safe(value)

        return json.dumps(payload, ensure_ascii=False, default=_json_safe)


class ConsoleFormatter(logging.Formatter):
    """Compact single-line format for local dev.

    Layout::

        HH:MM:SS LEVEL [request_id] [org_id/repo_id] logger | message  k=v k=v
    """

    def __init__(self) -> None:
        super().__init__()

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        ctx_ids = self._render_ids(record)
        msg = record.getMessage()
        extras = self._render_extras(record)

        line = f"{ts} {record.levelname:<5} {ctx_ids} {record.name} | {msg}"
        if extras:
            line = f"{line}  {extras}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line

    @staticmethod
    def _render_ids(record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", _DEFAULT)
        repo_id = getattr(record, "repo_id", _DEFAULT)
        org_id = getattr(record, "org_id", _DEFAULT)

        pairs = [f"req={_short(request_id)}"]
        if org_id != _DEFAULT:
            pairs.append(f"org={_short(org_id)}")
        if repo_id != _DEFAULT:
            pairs.append(f"repo={_short(repo_id)}")
        return "[" + " ".join(pairs) + "]"

    @staticmethod
    def _render_extras(record: logging.LogRecord) -> str:
        """Render non-correlation extras as ``k=v`` pairs."""
        parts = []
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_KEYS or key.startswith("_"):
                continue
            if key in _CORRELATION_FIELDS:
                continue
            parts.append(f"{key}={_compact_value(value)}")
        return " ".join(parts)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _iso_ts(record: logging.LogRecord) -> str:
    # UTC, millisecond precision, ``Z`` suffix — aligns with
    # DynamoDB's ``now_iso()`` so timestamps collate consistently
    # across storage and logs.
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        + f".{int(record.msecs):03d}Z"
    )


def _json_safe(value: Any) -> Any:
    """Return a JSON-serializable version of ``value``.

    Used both as the dict pass-through and as ``default=`` for
    :func:`json.dumps`. Falls back to :func:`repr` for anything exotic
    so a single weird field never kills the whole log line.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    # datetime, Path, Enum, Exception, etc.
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return repr(value)


def _short(value: str, n: int = 8) -> str:
    """Trim long uuids / hex ids for the console formatter."""
    if value == _DEFAULT:
        return value
    return value if len(value) <= n else value[:n]


def _compact_value(value: Any) -> str:
    """Single-line, quotation-free rendering for console ``k=v`` pairs."""
    if isinstance(value, str):
        if " " in value or "=" in value:
            return json.dumps(value, ensure_ascii=False)
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        return repr(value)


# --------------------------------------------------------------------------- #
# Semantic event helpers                                                      #
# --------------------------------------------------------------------------- #

def log_event(
    logger: logging.Logger,
    event: str,
    message: Optional[str] = None,
    *,
    level: int = logging.INFO,
    exc_info: Any = None,
    **fields: Any,
) -> None:
    """Emit a structured log record with an ``event`` key.

    ``event`` is the queryable handle — adopt dot-delimited names like
    ``repo_ingest.started``, ``git.clone.completed``, ``db.put_item``.
    ``message`` is the human string; if omitted it defaults to ``event``
    so console logs stay readable.

    Example::

        log_event(log, "git.clone.completed",
                  dest=str(dest), attempt=attempt, duration_ms=dur)
    """
    extra = _safe_log_extra({"event": event, **fields})
    logger.log(level, message or event, extra=extra, exc_info=exc_info)


def log_status_transition(
    logger: logging.Logger,
    *,
    entity: str,
    from_status: str,
    to_status: str,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit a standardised ``event=status.transition`` log line.

    Shape::

        event=status.transition entity=<entity> from=<x> to=<y> <extras>

    Any first-class correlation fields bound via :func:`log_context` are
    inherited automatically — callers only need to pass ``entity``,
    ``from_status``, ``to_status``, and anything domain-specific.
    """
    log_event(
        logger,
        "status.transition",
        f"{entity}: {from_status} -> {to_status}",
        level=level,
        entity=entity,
        from_status=from_status,
        to_status=to_status,
        **fields,
    )


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

def configure_logging(settings: Settings) -> None:
    """Configure the root logger. Safe to call more than once."""

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.addFilter(_ContextFilter())
    handler.setFormatter(
        JsonFormatter() if settings.log_format == LogFormat.JSON else ConsoleFormatter()
    )

    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Tame noisy libs; callers can still raise them explicitly.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Module-level convenience wrapper."""
    return logging.getLogger(name)


__all__ = [
    "ConsoleFormatter",
    "JsonFormatter",
    "configure_logging",
    "get_logger",
    "job_id_var",
    "log_context",
    "log_event",
    "log_status_transition",
    "org_id_var",
    "repo_id_var",
    "request_id_var",
]
