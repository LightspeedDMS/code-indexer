"""Request admission control / backpressure middleware.

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

import hashlib
import math
import threading
from typing import Iterable, Optional, Tuple

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from code_indexer.server.auth.token_bucket import TokenBucketManager

# Paths never subject to admission control: liveness/readiness + API schema.
# Prefix match, so e.g. "/docs" also covers "/docs/oauth2-redirect".
_DEFAULT_EXEMPT_PREFIXES = ("/health", "/docs", "/openapi.json", "/redoc")

# JWT session cookie name. Mirrors auth.dependencies.CIDX_SESSION_COOKIE; kept as
# a stable local literal so this request-path middleware doesn't import the heavy
# auth module (which would risk an import cycle at app-wiring time).
_SESSION_COOKIE = "cidx_session"

# Callers presenting no credential share a single bucket, so an unauthenticated
# flood is throttled as one group (auth rejects those requests downstream anyway).
_ANON_CONSUMER = "anon"


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


class PerConsumerRateLimiter:
    """Per-consumer token-bucket rate limiter (dealer-wide fairness).

    The global :class:`AdmissionController` cap is fleet-wide: one abusive client
    (e.g. a single dealer hammering the support agent) can consume every in-flight
    slot and get everyone else shed. This limiter throttles each caller
    INDIVIDUALLY, keyed by a hash of its presented credential, so a noisy consumer
    is capped without starving the rest. It reuses the tested ``TokenBucketManager``
    (auth/token_bucket) — ``capacity`` is the burst allowance and ``refill_per_second``
    the sustained rate; idle buckets are reclaimed after ``cleanup_seconds``.
    """

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        cleanup_seconds: int = 3600,
    ) -> None:
        self._manager = TokenBucketManager(
            capacity=capacity,
            refill_rate=refill_per_second,
            cleanup_seconds=cleanup_seconds,
        )

    @staticmethod
    def consumer_key(request: Request) -> str:
        """Derive a stable per-consumer key from the request credential.

        Prefers the ``Authorization`` header (Bearer JWT or ``cidx_sk_`` API key),
        then the JWT session cookie. The raw credential is never stored — only its
        SHA-256 hash — so buckets can't leak secrets. Credential-less requests map
        to a single shared anon bucket.
        """
        cred = request.headers.get("authorization")
        if not cred:
            cred = request.cookies.get(_SESSION_COOKIE)
        if not cred:
            return _ANON_CONSUMER
        return hashlib.sha256(cred.encode("utf-8")).hexdigest()[:32]

    def check(self, request: Request) -> Tuple[bool, float]:
        """Consume one token for this request's consumer.

        Returns ``(allowed, retry_after_seconds)``; ``retry_after`` is meaningful
        only when ``allowed`` is False.
        """
        allowed, retry_after = self._manager.consume(self.consumer_key(request))
        return allowed, retry_after


class AdmissionControlMiddleware(BaseHTTPMiddleware):
    """Sheds excess load with 429 + Retry-After.

    Two independent gates, either or both may be active:
      * ``rate_limiter`` (PerConsumerRateLimiter) — per-client fairness, checked
        FIRST so an abusive consumer is shed before it takes a global slot.
      * ``controller`` (AdmissionController) — global per-worker in-flight cap.
    """

    def __init__(
        self,
        app,
        controller: Optional[AdmissionController] = None,
        rate_limiter: Optional[PerConsumerRateLimiter] = None,
        exempt_prefixes: Iterable[str] = _DEFAULT_EXEMPT_PREFIXES,
    ) -> None:
        super().__init__(app)
        self._controller = controller
        self._rate_limiter = rate_limiter
        self._exempt = tuple(exempt_prefixes)

    def _is_exempt(self, path: str) -> bool:
        return path.startswith(self._exempt)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if self._is_exempt(request.url.path):
            return await call_next(request)

        # Per-consumer fairness FIRST: shed an over-rate client before it takes a
        # global in-flight slot from well-behaved clients.
        if self._rate_limiter is not None:
            allowed, retry_after = self._rate_limiter.check(request)
            if not allowed:
                # retry_after is inf when the bucket never refills (refill rate 0);
                # cap it so math.ceil() can't raise OverflowError on a misconfig.
                retry_secs = (
                    max(1, math.ceil(retry_after))
                    if math.isfinite(retry_after)
                    else 3600
                )
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(retry_secs)},
                    content={
                        "detail": (
                            "Per-client rate limit exceeded; retry after a short delay."
                        ),
                        "retry_after_seconds": retry_secs,
                    },
                )

        # Global per-worker in-flight cap.
        if self._controller is not None and not self._controller.try_enter():
            retry_after_s = self._controller.retry_after_seconds
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after_s)},
                content={
                    "detail": (
                        "Server at capacity (too many concurrent requests); "
                        "retry after a short delay."
                    ),
                    "retry_after_seconds": retry_after_s,
                },
            )
        try:
            return await call_next(request)
        finally:
            # Only release a slot we actually took (controller present + entered;
            # a shed request returned above before reaching here).
            if self._controller is not None:
                self._controller.leave()
