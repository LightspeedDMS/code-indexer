"""Security response headers on every HTTP response (pure ASGI middleware).

Every response gets ``Content-Security-Policy: frame-ancestors 'none'``,
``X-Frame-Options: DENY``, ``X-Content-Type-Options: nosniff`` and
``Referrer-Policy: same-origin``.  Responses under ``/wiki`` get the strict
``WIKI_CSP`` instead of the frame-only policy.  ``Strict-Transport-Security``
is added only when the request scheme the app sees is https (uvicorn sets it
from forwarded headers when the peer is a trusted proxy).

A header the route already set is kept, never overwritten.  Only the
``http.response.start`` message is touched; body messages are forwarded as
they arrive, so streaming responses and background tasks are unaffected
(unlike ``BaseHTTPMiddleware``).
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

FRAME_ANCESTORS_CSP = "frame-ancestors 'none'"

# Wiki pages render repository markdown.  Scripts only from this origin;
# style-src allows the Pico stylesheet the wiki base template loads and
# inline style attributes the wiki templates and rendered articles carry;
# img-src allows data: (stylesheet icons) and the absolute http(s) image
# URLs that rendered articles keep verbatim.
WIKI_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "style-src 'self' https://cdn.jsdelivr.net/npm/@picocss/pico@2/ 'unsafe-inline'; "
    "img-src 'self' data: https: http:"
)
WIKI_PATH_PREFIX = "/wiki"
HSTS_VALUE = "max-age=31536000"

_BASE_HEADERS = (
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
)


def _is_wiki_path(path: str) -> bool:
    return path == WIKI_PATH_PREFIX or path.startswith(WIKI_PATH_PREFIX + "/")


class SecurityHeadersMiddleware:
    """Add the security headers to every HTTP response start message."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        csp = WIKI_CSP if _is_wiki_path(scope.get("path", "")) else FRAME_ANCESTORS_CSP
        is_https = scope.get("scheme") == "https"

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", csp)
                for name, value in _BASE_HEADERS:
                    headers.setdefault(name, value)
                if is_https:
                    headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
