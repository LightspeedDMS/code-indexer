import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from .live_server import run_cli
from .secret_scan import REDACTED, Arg, Masked, files_containing

SECRET = Masked("synthetic-Echo-0000-example-only")
USER = Masked("synthetic-User-0000-example-only")
TOKEN = Masked("synthetic-Token-0000-example-only")
ECHO = " ".join(s.value for s in (SECRET, USER, TOKEN))
CLI_TIMEOUT_S = 120
CORPUS = """\
top_n: 10
xray_evaluator: "fn evaluate_node() {}"
repos:
  - url: https://example.com/a/b
    probe: {query: Probe}
    queries:
      - {mode: fts, query: Foo, expect: a.py}
      - {mode: regex, query: Foo, expect: a.py}
      - {mode: xray, query: Foo, include: ["*.py"], expect: a.py}
"""


class _EchoHandler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: Dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _tool(self, payload: Dict[str, Any]) -> None:
        text = json.dumps(payload)
        self._send(
            200, {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": text}]}}
        )

    def do_GET(self) -> None:
        echo = ECHO
        if self.path == "/health":
            self._send(200, {"status": f"degraded {echo}", "version": f"9.9 {echo}"})
        elif self.path.startswith("/api/jobs/"):
            self._send(200, {"status": "failed", "error": f"evaluator crashed {echo}"})
        else:
            self._send(404, {"detail": "no route"})

    def do_POST(self) -> None:
        echo = ECHO
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/auth/login":
            self._send(200, {"access_token": TOKEN.value})
        elif self.path == "/api/query" and body.get("limit") == 1:
            self._send(200, {"fts_results": [{"path": "a.py"}]})
        elif self.path == "/api/query":
            self._send(500, {"detail": f"query failed for {echo}"})
        elif self.path == "/api/regex/search":
            self._send(200, {"matches": [{"file_path": f"{echo}.py"}]})
        elif self.path == "/api/xray/search":
            self._send(202, {"job_id": "job-1"})
        elif self.path == "/mcp":
            self._mcp(body["params"]["name"], echo)
        else:
            self._send(404, {"detail": "no route"})

    def _mcp(self, tool: str, echo: str) -> None:
        if tool == "list_global_repos":
            row = {
                "alias_name": "example-repo-global",
                "repo_url": "https://example.com/a/b",
            }
            self._tool({"repos": [row]})
        elif tool == "xray_search":
            self._tool({"job_id": "job-1"})
        elif tool == "get_job_details":
            self._tool({"job": {"status": "failed", "error": f"crashed {echo}"}})
        else:
            self._tool({"success": False, "error": f"tool failed for {echo}"})

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def echo_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_server_echoed_password_never_reaches_terminal_or_reports(
    echo_url: str, tmp_path: Path
):
    corpus = tmp_path / "corpus.yaml"
    corpus.write_text(CORPUS)
    out_dir = tmp_path / "out"
    argv: List[Arg] = ["--url", echo_url, "--username", USER]
    argv += ["--password", SECRET, "--quick", "--corpus", str(corpus)]
    argv += ["--output-dir", str(out_dir)]
    done = run_cli(argv, timeout=CLI_TIMEOUT_S)
    assert done.returncode == 1, done
    for value in (SECRET, USER, TOKEN):
        leaked_to_terminal = (done.stdout + done.stderr).count(value.value)
        assert leaked_to_terminal == 0, value
        assert files_containing(value, [out_dir]) == [], value
    saved = (next(out_dir.glob("perf_*.json"))).read_text()
    markdown = (next(out_dir.glob("perf_*.md"))).read_text()
    redactions = saved.count(REDACTED)
    assert redactions > 0, "the echoed texts must have been recorded, redacted"
    local_paths = (saved + markdown).count(str(tmp_path))
    assert local_paths == 0
