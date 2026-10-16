"""A local HTTP git server that requires HTTP Basic credentials.

Serves the bare repositories under ``project_root`` through the real
``git http-backend`` (CGI). A request without the expected credentials is
answered 401, so a git command succeeds against it only when it supplies
them. Binds to 127.0.0.1 on an ephemeral port.
"""

from __future__ import annotations

import base64
import binascii
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator, List, Tuple

_HTTP_BACKEND_TIMEOUT_SECONDS = 60


def _git_http_backend() -> str:
    exec_path = subprocess.run(
        ["git", "--exec-path"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return str(Path(exec_path) / "git-http-backend")


def _basic_credentials(header: str) -> Tuple[str, str]:
    """(username, password) decoded from a Basic Authorization header, or
    ("", "") when the header is absent or malformed."""
    scheme, _, encoded = header.partition(" ")
    if scheme != "Basic":
        return "", ""
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return "", ""
    user, _, secret = decoded.partition(":")
    return user, secret


def _parse_cgi_head(head: bytes) -> Tuple[int, List[Tuple[str, str]]]:
    """Status code (200 when the CGI output names none) and the other
    headers of a CGI response head."""
    status = 200
    headers: List[Tuple[str, str]] = []
    for line in head.decode("latin-1").split("\r\n"):
        name, _, value = line.partition(":")
        if name.lower() == "status":
            code = value.strip().split()
            if code and code[0].isdigit():
                status = int(code[0])
        elif name:
            headers.append((name, value.strip()))
    return status, headers


@contextmanager
def serve_authenticated_git(
    project_root: Path, username: str, password: str
) -> Iterator[str]:
    """Yield ``http://127.0.0.1:<port>``; ``<base>/<name>.git`` serves
    ``project_root/<name>.git``."""
    backend = _git_http_backend()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # keep test output quiet
            pass

        def _reply(
            self, status: int, headers: List[Tuple[str, str]], body: bytes
        ) -> None:
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve(self) -> None:
            supplied = _basic_credentials(self.headers.get("Authorization", ""))
            if supplied != (username, password):
                self._reply(401, [("WWW-Authenticate", 'Basic realm="example"')], b"")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path, _, query = self.path.partition("?")
            env = {
                "GIT_PROJECT_ROOT": str(project_root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "REQUEST_METHOD": self.command,
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(length),
                "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                "REMOTE_USER": username,
                "REMOTE_ADDR": "127.0.0.1",
            }
            result = subprocess.run(
                [backend],
                input=body,
                env=env,
                capture_output=True,
                timeout=_HTTP_BACKEND_TIMEOUT_SECONDS,
                check=False,
            )
            if result.returncode != 0:
                self._reply(500, [("Content-Type", "text/plain")], result.stderr)
                return
            head, _, payload = result.stdout.partition(b"\r\n\r\n")
            status, headers = _parse_cgi_head(head)
            self._reply(status, headers, payload)

        do_GET = _serve
        do_POST = _serve

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@contextmanager
def served_bare_remote(tmp_path: Path, username: str, password: str) -> Iterator[str]:
    """Yield the credential-free URL of a bare repository holding one commit
    (README.md) on ``main`` that is reachable only with ``username`` and
    ``password``."""
    work = tmp_path / "remote-work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    (work / "README.md").write_text("example\n")
    _git("add", "README.md", cwd=work)
    _git(
        "-c",
        "user.name=Example",
        "-c",
        "user.email=e@example.com",
        "commit",
        "-m",
        "init",
        cwd=work,
    )
    served = tmp_path / "served"
    served.mkdir()
    _git("clone", "--bare", str(work), str(served / "remote.git"), cwd=tmp_path)
    with serve_authenticated_git(served, username, password) as base:
        yield f"{base}/remote.git"
