"""Request admission control / backpressure middleware (C7).

Without this, every request queues in the anyio threadpool under overload until
it times out to a 504 -- the documented shared-queue failure mode. This
middleware caps the number of in-flight (non-exempt) requests PER WORKER PROCESS
and sheds excess immediately with ``429 Too Many Requests`` + ``Retry-After`` so
clients back off and retry instead of piling up toward the queue-collapse point.

Scope is per worker process (each uvicorn worker has its own counter), which is
the natural unit: a worker sheds load when *its* in-flight set is full, so total
pod capacity is ``workers x max_inflight_requests``.

Health/docs endpoints are always exempt so readiness probes (which must keep
returning 200 or k8s pulls the pod from the Service) and schema fetches are
never rejected.
"""

from __future__ import annotations

import threading
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Paths never subject to admission control: liveness/readiness + API schema.
# Prefix match, so e.g. "/docs" also covers "/docs/oauth2-redirect".
_DEFAULT_EXEMPT_PREFIXES = ("/health", "/docs", "/openapi.json", "/redoc")


class AdmissionController:
    """Thread-safe per-process in-flight counter with a hard cap.

    ``try_enter`` returns False (shed) when the cap is reached; callers that
    entered must always ``leave`` (in a finally) so the slot is released.
    """

    def __init__(self, max_inflight: int, retry_after_seconds: int) -> None:
        self._max = max_inflight
        self.retry_after_seconds = retry_after_seconds
        self._inflight = 0
        self._lock = threading.Lock()

    def try_enter(self) -> bool:
        with self._lock:
            # max <= 0 means "no cap" -- admit everything.
            if self._max > 0 and self._inflight >= self._max:
                return False
            self._inflight += 1
            return True

    def leave(self) -> None:
        with self._lock:
            if self._inflight > 0:
                self._inflight -= 1

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight


class AdmissionControlMiddleware(BaseHTTPMiddleware):
    """Sheds excess in-flight requests with 429 + Retry-After (C7)."""

    def __init__(
        self,
        app,
        controller: AdmissionController,
        exempt_prefixes: Iterable[str] = _DEFAULT_EXEMPT_PREFIXES,
    ) -> None:
        super().__init__(app)
        self._controller = controller
        self._exempt = tuple(exempt_prefixes)

    def _is_exempt(self, path: str) -> bool:
        return path.startswith(self._exempt)

    async def dispatch(self, request: Request, call_next) -> Response:
        if self._is_exempt(request.url.path):
            return await call_next(request)

        if not self._controller.try_enter():
            retry_after = self._controller.retry_after_seconds
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={
                    "detail": (
                        "Server at capacity (too many concurrent requests); "
                        "retry after a short delay."
                    ),
                    "retry_after_seconds": retry_after,
                },
            )
        try:
            return await call_next(request)
        finally:
            self._controller.leave()
