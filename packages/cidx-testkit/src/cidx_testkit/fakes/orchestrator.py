"""A fake agent orchestrator: the remote service the CLI invoker delegates to.

The orchestrator delegate (``plugins/cidx_orchestrator_invoker``) talks plain
HTTP, so this fake is a real HTTP server on the loopback interface (an
ephemeral port), run in a thread: the delegate under test is exercised exactly
as deployed, with nothing patched. It serves the three routes the delegate
uses:

* ``POST /auth/login``  ``{username, password}`` -> ``{token}`` (401 on a wrong
  password);
* ``POST /jobs/with-capabilities`` (Bearer token) -> ``{jobId}``; 401 for an
  unknown or revoked token, 429 with ``retryAfterSeconds`` while
  :attr:`FakeOrchestrator.capacity_rejections` is positive;
* ``GET /jobs/{jobId}`` (Bearer token) -> ``{status, result}``: ``running``
  for :attr:`polls_before_terminal` polls, then the configured outcome.

What a job does is set per run with :meth:`succeed_with`, :meth:`fail_with`
or :meth:`never_finish`; :meth:`go_away` makes the service unreachable. Every
request is recorded (:attr:`requests`) and every submitted job body kept
(:attr:`submitted`).
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Set, Tuple

from cidx_sdk.seams.cli_invoker import CliInvoker

from cidx_testkit.conformance.cli_invoker import CliInvokerSubject

#: The fake listens on the IPv4 loopback interface only.
_LOOPBACK = "127.0.0.1"
#: The password the fake accepts (set it as the delegate's credential).
FAKE_PASSWORD = "example-password"


@dataclass
class _Outcome:
    status: str = "completed"
    output: str = ""
    error: str = ""
    exit_code: int = 0
    finishes: bool = True


@dataclass
class FakeOrchestrator:
    """The service state. Start it with :meth:`start` (or use it as a context
    manager); :attr:`url` is its base URL once started."""

    password: str = FAKE_PASSWORD
    polls_before_terminal: int = 1
    capacity_rejections: int = 0
    retry_after_seconds: int = 0
    requests: List[Tuple[str, str]] = field(default_factory=list)
    submitted: List[Dict[str, Any]] = field(default_factory=list)
    _outcome: _Outcome = field(default_factory=_Outcome)
    _tokens: Set[str] = field(default_factory=set)
    _jobs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _server: Optional[ThreadingHTTPServer] = None
    _thread: Optional[threading.Thread] = None
    _gone_url: Optional[str] = None

    # -- outcomes --------------------------------------------------------------

    def succeed_with(self, output: str) -> None:
        """Jobs submitted from now on complete with *output*, exit code 0."""
        self._outcome = _Outcome(status="completed", output=output)

    def fail_with(self, error: str, status: str = "failed") -> None:
        """Jobs submitted from now on end *status* with *error*."""
        self._outcome = _Outcome(status=status, error=error, exit_code=1)

    def never_finish(self) -> None:
        """Jobs submitted from now on stay ``running``."""
        self._outcome = _Outcome(finishes=False)

    def revoke_tokens(self) -> None:
        """Forget every issued token: the next authenticated call answers 401."""
        with self._lock:
            self._tokens.clear()

    # -- lifecycle -------------------------------------------------------------

    @property
    def url(self) -> str:
        if self._gone_url is not None:
            return self._gone_url
        if self._server is None:
            raise RuntimeError("the fake orchestrator is not started")
        # Bound to the IPv4 loopback above, so the host is that literal.
        return f"http://{_LOOPBACK}:{self._server.server_port}"

    def start(self) -> "FakeOrchestrator":
        handler = _handler_for(self)
        self._server = ThreadingHTTPServer((_LOOPBACK, 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-orchestrator", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def go_away(self) -> None:
        """Stop serving but keep :attr:`url`: callers now get a refused
        connection, as from a service that is down."""
        url = self.url
        self.stop()
        self._gone_url = url

    def __enter__(self) -> "FakeOrchestrator":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- request handling (called on server threads) ----------------------------

    def _login(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        if body.get("password") != self.password:
            return 401, {"error": "invalid credentials"}
        token = uuid.uuid4().hex
        with self._lock:
            self._tokens.add(token)
        return 200, {"token": token}

    def _authorized(self, header: Optional[str]) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        with self._lock:
            return header[len("Bearer ") :] in self._tokens

    def _submit(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        with self._lock:
            if self.capacity_rejections > 0:
                self.capacity_rejections -= 1
                return 429, {"retryAfterSeconds": self.retry_after_seconds}
            job_id = str(body.get("jobId") or uuid.uuid4())
            self.submitted.append(body)
            self._jobs[job_id] = {"polls": 0, "outcome": self._outcome}
        return 201, {"jobId": job_id}

    def _poll(self, job_id: str) -> Tuple[int, Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return 404, {"error": f"no job {job_id}"}
            job["polls"] += 1
            outcome: _Outcome = job["outcome"]
            if not outcome.finishes or job["polls"] <= self.polls_before_terminal:
                return 200, {"status": "running", "result": None}
        result = {"status": outcome.status, "exitCode": outcome.exit_code}
        if outcome.output:
            result["output"] = outcome.output
        if outcome.error:
            result["error"] = outcome.error
        return 200, {"status": outcome.status, "result": result}

    def handle(
        self, method: str, path: str, authorization: Optional[str], raw: bytes
    ) -> Tuple[int, Dict[str, Any]]:
        self.requests.append((method, path))
        body: Dict[str, Any] = json.loads(raw) if raw else {}
        if method == "POST" and path == "/auth/login":
            return self._login(body)
        if not self._authorized(authorization):
            return 401, {"error": "unauthorized"}
        if method == "POST" and path == "/jobs/with-capabilities":
            return self._submit(body)
        if method == "GET" and path.startswith("/jobs/"):
            return self._poll(path[len("/jobs/") :])
        return 404, {"error": f"no route {method} {path}"}


def _handler_for(service: FakeOrchestrator) -> type:
    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            status, payload = service.handle(
                method, self.path, self.headers.get("Authorization"), raw
            )
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._answer("GET")

        def do_POST(self) -> None:
            self._answer("POST")

        def log_message(self, format: str, *args: Any) -> None:
            """Silence the default stderr access log."""

    return Handler


def orchestrator_subject(
    invoker: CliInvoker, service: FakeOrchestrator, cwd: str
) -> CliInvokerSubject:
    """A CLI invoker conformance subject for *invoker*, a delegate pointed at
    the started *service*."""
    return CliInvokerSubject(
        invoker=invoker,
        cwd=cwd,
        succeed=service.succeed_with,
        fail=service.fail_with,
        unreachable=service.go_away,
        hang=service.never_finish,
    )
