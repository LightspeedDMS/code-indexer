import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from perf_suite import cli, client, corpus, operations, report

from .secret_scan import Masked, ScanError, files_containing, scan_file

WORKERS = 16
SYNTHETIC = "synthetic-Key-0000-example-only"


class _Server(ThreadingHTTPServer):
    logins = 0
    lock = threading.Lock()
    login_body = b'{"access_token": "example-token"}'
    mcp_body = b"not json at all"


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/auth/login":
            with self.server.lock:
                self.server.logins += 1
                first = self.server.logins == 1
            self._reply(
                200 if first else 401, self.server.login_body if first else b"{}"
            )
        else:
            self._reply(200, self.server.mcp_body)

    def do_GET(self) -> None:
        self._reply(200, b'{"status": "healthy", "version": "1"}')

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def server() -> Iterator[_Server]:
    srv = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _url(srv: _Server) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}"


def test_rejected_relogin_is_attempted_once_across_workers(server: _Server):
    perf = client.PerfClient(
        _url(server), "example-user", "example-pass", http_timeout=5
    )
    perf.login()
    perf._token_obtained_at = float("-inf")
    outcomes: List[str] = []

    def worker() -> None:
        try:
            perf.rest("GET", "/health")
            outcomes.append("ok")
        except client.TargetUnreachableError:
            outcomes.append("login lost")

    threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert server.logins == 2
    assert outcomes == ["login lost"] * WORKERS


def test_login_request_is_bounded():
    unbounded = client.PerfClient("http://192.0.2.1:9", "u", "p")
    assert unbounded.login_timeout() == client.LOGIN_TIMEOUT_S
    bounded = client.PerfClient("http://192.0.2.1:9", "u", "p", http_timeout=0.5)
    assert bounded.login_timeout() == 0.5


def test_resolve_aliases_skips_unusable_rows():
    rows: List[Dict[str, Any]] = [
        {"repo_url": "https://example.com/a/b"},
        {"alias_name": "odd-global", "repo_url": 5},
        {"alias_name": "good-global", "repo_url": "https://example.com/c/d.git"},
    ]
    resolved, missing = corpus.resolve_aliases(["https://example.com/c/d"], rows)
    assert resolved == {"https://example.com/c/d": "good-global"} and missing == []


def test_non_json_mcp_response_is_a_bounded_shape_error(server: _Server):
    server.mcp_body = b"x" * 5000
    perf = client.PerfClient(_url(server), "u", "p", http_timeout=5)
    perf.login()
    with pytest.raises(client.ShapeError) as raised:
        perf.mcp("search_code", {})
    assert len(str(raised.value)) < 600


def test_non_json_login_response_is_a_rejected_login(server: _Server):
    server.login_body = b"<html>not json</html>"
    perf = client.PerfClient(_url(server), "u", "p", http_timeout=5)
    with pytest.raises(client.LoginRejectedError):
        perf.login()


@pytest.mark.parametrize(
    "parse",
    [
        operations.rest_multi_paths,
        operations.mcp_multi_paths,
        operations.regex_multi_paths,
    ],
)
def test_non_object_multi_payload_is_a_shape_error(parse: Any):
    with pytest.raises(client.ShapeError):
        parse(["unexpected"])


def _failing_report() -> Dict[str, Any]:
    stats = {
        "requests": 4,
        "served": 4,
        "rps": 1.0,
        "p50_ms": 10.0,
        "p95_ms": 10.0,
        "p99_ms": 10.0,
        "errors": 0,
        "wrong": 1,
    }
    scenario = {
        "concurrency": 1,
        "warmup_ms": [],
        "warmup_errors": 0,
        "warmup_wrong": 0,
        "wrong_samples": ["q missed"],
        "repetitions": [],
        "median": stats,
        "mad": {k: 0.0 for k in stats},
        "aborted": None,
    }
    rep: Dict[str, Any] = report.build_report(
        label="example",
        server_version="1",
        registered_repo_count=1,
        corpus_repos=[],
        health_before="healthy",
        health_after="healthy",
        settings={},
        duration_s=1.0,
        scenarios={"fts": {"rest": {"1": scenario}, "mcp": None}},
        request_tally={},
    )
    return rep


@pytest.mark.parametrize(
    "baseline",
    [
        {"scenarios": {}},
        {"scenarios": {"other/rest/c1": {"p50_ms": 1.0, "p50_mad": 0.0, "rps": 1.0}}},
    ],
)
def test_baseline_comparing_nothing_is_rejected(baseline: Dict[str, Any]):
    rep = _failing_report()
    rep["scenarios"]["fts"]["rest"]["1"]["median"]["wrong"] = 0
    with pytest.raises(report.BaselineMismatchError):
        report.compare_to_baseline(rep, baseline, 10.0)


def test_failing_run_never_writes_a_baseline(tmp_path: Path):
    target = tmp_path / "base.json"
    code, summary = cli.conclude(
        _failing_report(), None, 10.0, tmp_path, target, False, False
    )
    assert code == 2
    assert not target.exists()
    assert "baseline not written" in summary


def test_vanished_root_and_file_are_skipped(tmp_path: Path):
    assert files_containing(Masked(SYNTHETIC), [tmp_path / "gone"]) == []
    assert scan_file(tmp_path / "gone.log", Masked(SYNTHETIC), 1024) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read chmod 000 dirs")
def test_unreadable_directory_fails_loudly(tmp_path: Path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(ScanError, match="locked: unreadable"):
            files_containing(Masked(SYNTHETIC), [tmp_path])
    finally:
        locked.chmod(0o700)


@pytest.mark.parametrize("status", ["interrupted", "completed_partial"])
def test_non_completed_terminal_job_is_an_error(status: str):
    with pytest.raises(client.RequestFailedError, match=status):
        operations.xray_job_paths({"status": status, "error": None})


def test_terminal_job_json_round_trip():
    assert json.loads(json.dumps(list(operations.TERMINAL_STATUSES)))
