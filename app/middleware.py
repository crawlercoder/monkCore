"""HTTP middleware: per-request id + structured access logs."""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.logging import get_logger, log_event, request_id_var

log = get_logger("app.access")

_HEADER = "x-request-id"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, propagates it via ContextVar, and logs access.

    The ``request_id`` ContextVar seeds the correlation context for every
    log line emitted during a request — services and DB modules don't
    need to know anything about HTTP to pick it up.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(_HEADER) or uuid.uuid4().hex
        token = request_id_var.set(request_id)

        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[_HEADER] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000.0, 2)
            # One structured access log per request. ``event=http.request``
            # is the queryable handle; ``status`` / ``duration_ms`` feed
            # dashboards without parsing message text.
            log_event(
                log,
                "http.request",
                f"{request.method} {request.url.path} -> {status_code} ({duration_ms}ms)",
                method=request.method,
                path=request.url.path,
                status=status_code,
                duration_ms=duration_ms,
                client=request.client.host if request.client else None,
            )
            request_id_var.reset(token)
