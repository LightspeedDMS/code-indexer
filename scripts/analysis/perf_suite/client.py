import json
import re
import threading
import time
from collections import Counter
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlsplit

import httpx

REDACTED = "<redacted>"
TARGET = "<target>"
# ^ why re-login after 8 min, not on 401; tokens expire at 10 min and a retried login feeds the throttle
TOKEN_REFRESH_AFTER_S = 8 * 60
CONNECT_TIMEOUT_S = 30.0
LOGIN_TIMEOUT_S = 30.0
MAX_CONNECTIONS = 64
ERROR_EXCERPT_CHARS = 300
MIN_REDACT_CHARS = 3
LOGIN_LOST = "login lost: token refresh rejected; not retried"
# ^ invariant this many consecutive connection-level failures = target gone; nothing more is sent
CONNECTION_LOSS_LIMIT = 20
# ^ invariant connection-level only; HTTP statuses and read/write timeouts are not "target gone"
CONNECTION_LOST_ERRORS = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "CloseError",
    }
)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")

# ^ security only these doors may be called; every one is read-only against the target
ALLOWED_REST = frozenset(
    {
        ("POST", "/auth/login"),
        ("GET", "/health"),
        ("POST", "/api/query"),
        ("POST", "/api/query/multi"),
        ("POST", "/api/regex/search"),
        ("POST", "/api/xray/search"),
        ("GET", "/api/jobs/{job_id}"),
    }
)
ALLOWED_MCP_TOOLS = frozenset(
    {
        "list_global_repos",
        "search_code",
        "regex_search",
        "xray_search",
        "get_job_details",
        "list_files",
        "get_file_content",
    }
)


def scrub(text: str) -> str:
    return _IPV4.sub("<address>", _URL.sub("<url>", text))


class NotAllowedError(Exception):
    pass


class LoginRejectedError(Exception):
    pass


class TransportError(Exception):
    def __init__(
        self, message: str, kind: str = "TransportError", connection_lost: bool = False
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.connection_lost = connection_lost


class RequestFailedError(Exception):
    def __init__(self, message: str, kind: str = "RequestFailedError") -> None:
        super().__init__(message)
        self.kind = kind


class AbortScenarioError(Exception):
    pass


class ShapeError(AbortScenarioError):
    pass


class PollFailedError(AbortScenarioError):
    pass


class TargetUnreachableError(AbortScenarioError):
    pass


class _Secret:
    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return REDACTED


class PerfClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        http_timeout: Optional[float] = None,
    ) -> None:
        if not password:
            raise ValueError("password must not be empty")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.http_timeout = http_timeout
        self._password = _Secret(password)
        self._host = urlsplit(self.base_url).hostname or ""
        timeout = httpx.Timeout(http_timeout, connect=http_timeout or CONNECT_TIMEOUT_S)
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            limits=httpx.Limits(max_connections=MAX_CONNECTIONS),
        )
        self._lock = threading.Lock()
        self._login_lock = threading.Lock()
        self._tally: Counter = Counter()
        self._token: Optional[str] = None
        self._login_failure: Optional[str] = None
        self._token_obtained_at = 0.0
        self._lost_streak = 0
        self.unreachable: Optional[str] = None

    def __repr__(self) -> str:
        return f"PerfClient(base_url={self.base_url!r}, username={self.username!r})"

    def close(self) -> None:
        self._http.close()

    def redact(self, text: str) -> str:
        # ^ invariant longest first, skip < MIN_REDACT_CHARS; a short value would mangle every match
        held = [self._password.value, self._token, self.username]
        secrets = [s for s in held if isinstance(s, str) and len(s) >= MIN_REDACT_CHARS]
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return text

    def sanitize(self, text: str) -> str:
        text = self.redact(text).replace(self.base_url, TARGET)
        if self._host:
            text = text.replace(self._host, TARGET)
        return scrub(text)

    def tally(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._tally)

    def _count(self, key: str) -> None:
        with self._lock:
            self._tally[key] += 1

    def _excerpt(self, response: httpx.Response) -> str:
        return self.sanitize(response.text[:ERROR_EXCERPT_CHARS])

    def login_timeout(self) -> float:
        return self.http_timeout or LOGIN_TIMEOUT_S

    def login(self) -> None:
        # ^ invariant one failed login ends all logins; the server throttle punishes retries
        if self._login_failure is not None:
            raise LoginRejectedError(self._login_failure)
        token = self._request_token()
        if token is None:
            raise LoginRejectedError(str(self._login_failure))
        self._token = token
        self._token_obtained_at = time.monotonic()

    def _request_token(self) -> Optional[str]:
        self._count("POST /auth/login")
        try:
            response = self._http.post(
                "/auth/login",
                json={"username": self.username, "password": self._password.value},
                timeout=httpx.Timeout(self.login_timeout()),
            )
        except httpx.HTTPError as exc:
            self._login_failure = (
                f"login to {self.base_url} failed ({type(exc).__name__}: "
                f"{self.redact(str(exc))}); not retried"
            )
            return None
        if response.status_code != 200:
            self._login_failure = (
                f"login rejected by {self.base_url} with HTTP {response.status_code}: "
                f"{self._excerpt(response)}; not retried"
            )
            return None
        try:
            token = response.json().get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not isinstance(token, str) or not token:
            self._login_failure = (
                f"login response from {self.base_url} carries no access_token: "
                f"{self._excerpt(response)}; not retried"
            )
            return None
        return token

    def _headers(self) -> Dict[str, str]:
        with self._login_lock:
            age = time.monotonic() - self._token_obtained_at
            if self._token is None or age >= TOKEN_REFRESH_AFTER_S:
                refreshing = self._token is not None
                try:
                    self.login()
                except LoginRejectedError:
                    if not refreshing:
                        raise
                    # ^ invariant a refresh lost mid-run ends the run like a vanished target (report, exit 1)
                    with self._lock:
                        self.unreachable = self.unreachable or LOGIN_LOST
                    raise TargetUnreachableError(self.unreachable) from None
            return {"Authorization": f"Bearer {self._token}"}

    def _note_outcome(self, lost: bool) -> Optional[str]:
        with self._lock:
            if not lost:
                self._lost_streak = 0
                return None
            self._lost_streak += 1
            if self._lost_streak >= CONNECTION_LOSS_LIMIT and self.unreachable is None:
                self.unreachable = (
                    f"target unreachable: {self._lost_streak} consecutive "
                    "connection failures"
                )
            return self.unreachable

    def _send(
        self,
        method: str,
        path: str,
        key: str,
        body: Optional[Mapping[str, Any]],
        timeout: Optional[float] = None,
        breaker: bool = True,
    ) -> httpx.Response:
        if breaker and self.unreachable is not None:
            raise TargetUnreachableError(self.unreachable)
        headers = self._headers()
        self._count(key)
        request_timeout = (
            self._http.timeout if timeout is None else httpx.Timeout(timeout)
        )
        try:
            response = self._http.request(
                method, path, headers=headers, json=body, timeout=request_timeout
            )
        except httpx.HTTPError as exc:
            kind = type(exc).__name__
            lost = kind in CONNECTION_LOST_ERRORS
            gone = self._note_outcome(lost)
            if breaker and gone is not None:
                raise TargetUnreachableError(gone) from None
            raise TransportError(
                f"{key}: {kind}: {self.sanitize(str(exc))}",
                kind=kind,
                connection_lost=lost,
            ) from None
        self._note_outcome(False)
        return response

    def rest(
        self,
        method: str,
        template: str,
        path_params: Optional[Mapping[str, str]] = None,
        json: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        breaker: bool = True,
    ) -> Any:
        key = f"{method} {template}"
        if (method, template) not in ALLOWED_REST or template == "/auth/login":
            raise NotAllowedError(f"{key} is not on the read-only allow-list")
        path = template.format(**(path_params or {}))
        response = self._send(method, path, key, json, timeout, breaker)
        if not 200 <= response.status_code < 300:
            raise RequestFailedError(
                f"{key} HTTP {response.status_code}: {self._excerpt(response)}",
                kind=f"HTTP {response.status_code}",
            )
        try:
            return response.json()
        except ValueError:
            raise ShapeError(
                f"{key} returned non-JSON: {self._excerpt(response)}"
            ) from None

    def mcp(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        key = f"MCP {tool}"
        if tool not in ALLOWED_MCP_TOOLS:
            raise NotAllowedError(f"{key} is not on the read-only allow-list")
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": dict(arguments)},
        }
        response = self._send("POST", "/mcp", key, body, timeout)
        if response.status_code != 200:
            raise RequestFailedError(
                f"{key} HTTP {response.status_code}: {self._excerpt(response)}",
                kind=f"HTTP {response.status_code}",
            )
        return self._tool_payload(key, response)

    def _tool_payload(self, key: str, response: httpx.Response) -> Dict[str, Any]:
        try:
            envelope = response.json()
        except ValueError:
            raise ShapeError(
                f"{key} returned non-JSON: {self._excerpt(response)}"
            ) from None
        if not isinstance(envelope, dict):
            raise ShapeError(f"{key} JSON-RPC envelope is not an object")
        if "error" in envelope:
            raise RequestFailedError(
                f"{key} JSON-RPC error: {self.sanitize(str(envelope['error']))[:ERROR_EXCERPT_CHARS]}",
                kind="JSON-RPC error",
            )
        result = envelope.get("result")
        content = result.get("content") if isinstance(result, dict) else None
        if (
            not isinstance(content, list)
            or not content
            or not isinstance(content[0], dict)
            or not isinstance(content[0].get("text"), str)
        ):
            raise ShapeError(f"{key} result has no content[0].text")
        try:
            payload = json.loads(content[0]["text"])
        except ValueError:
            raise ShapeError(f"{key} content text is not JSON") from None
        if not isinstance(payload, dict):
            raise ShapeError(f"{key} tool payload is not an object")
        if payload.get("success") is False or (
            isinstance(result, dict) and result.get("isError")
        ):
            detail = payload.get("error") or payload.get("message") or payload
            raise RequestFailedError(
                f"{key} tool error: {self.sanitize(str(detail))[:ERROR_EXCERPT_CHARS]}",
                kind="tool error",
            )
        return payload
