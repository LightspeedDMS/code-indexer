import json
import os
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pytest

from perf_suite import cli, client
from perf_suite.corpus import load_corpus

from .conftest import PROJECT_ROOT

RUN_PY = PROJECT_ROOT / "scripts" / "analysis" / "perf_suite" / "run.py"
UNROUTABLE_URL = "http://192.0.2.1:9"
CORPUS = """\
top_n: 10
repos:
  - url: https://example.com/a/b
    probe: {query: Probe}
    queries:
      - {mode: fts, query: Foo, expect: a.py}
      - {mode: regex, query: Foo, expect: a.py}
"""


@pytest.mark.parametrize("stdin_text", [None, "\n"], ids=["eof", "empty-line"])
def test_empty_or_missing_password_is_a_config_error(stdin_text: Optional[str]):
    env = {k: v for k, v in os.environ.items() if k != cli.PASSWORD_ENV}
    done = subprocess.run(
        [sys.executable, str(RUN_PY), "--url", UNROUTABLE_URL, "--username", "u"]
        + ["--password", ""],
        input=stdin_text,
        stdin=subprocess.DEVNULL if stdin_text is None else None,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert done.returncode == 2
    assert "perf suite: a non-empty password is required" in done.stderr
    assert "Traceback" not in done.stderr and "requests sent" not in done.stderr


class _RefreshRejectingHandler(BaseHTTPRequestHandler):
    lock = threading.Lock()
    logins = 0

    def _send(self, code: HTTPStatus, body: Dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(HTTPStatus.OK, {"status": "healthy", "version": "9.9"})
        else:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": "not served"})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path != "/auth/login":
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": "not served"})
            return
        with type(self).lock:
            type(self).logins += 1
            first = type(self).logins == 1
        if first:
            self._send(HTTPStatus.OK, {"access_token": "example-token"})
        else:
            self._send(HTTPStatus.UNAUTHORIZED, {"detail": "login refused"})

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def refusing_url() -> Iterator[str]:
    with _RefreshRejectingHandler.lock:
        _RefreshRejectingHandler.logins = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RefreshRejectingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_failed_token_refresh_mid_run_writes_report_and_exits_1(
    refusing_url: str, tmp_path: Path
):
    corpus_path = tmp_path / "corpus.yaml"
    corpus_path.write_text(CORPUS)
    out_dir = tmp_path / "out"
    args = cli.parse_args(
        ["--url", refusing_url, "--username", "u", "--password", "example-pass"]
        + ["--quick", "--corpus", str(corpus_path), "--output-dir", str(out_dir)]
    )
    perf = client.PerfClient(refusing_url, "u", "example-pass")
    try:
        perf.login()
        health_before, version = cli._health(perf)
        perf._token_obtained_at = float("-inf")
        aliases = {"https://example.com/a/b": "example-repo-global"}
        code = cli.measure_and_report(
            perf,
            load_corpus(corpus_path),
            aliases,
            1,
            health_before,
            version,
            None,
            args,
        )
        tally = perf.tally()
    finally:
        perf.close()
    assert code == cli.EXIT_FAIL
    assert tally["POST /auth/login"] == 2
    saved = json.loads(next(out_dir.glob("perf_*.json")).read_text())
    assert "login lost" in saved["target_unreachable"]
    assert any("login lost" in r for r in saved["verdict"]["reasons"])
    assert saved["health_after"].startswith("unavailable")
    cells = [
        cell
        for doors in saved["scenarios"].values()
        for by_conc in doors.values()
        for cell in (by_conc or {}).values()
    ]
    assert any(c["aborted"] == cli.NOT_RUN_UNREACHABLE for c in cells)
