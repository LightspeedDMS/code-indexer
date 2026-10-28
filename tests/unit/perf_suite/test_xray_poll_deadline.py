import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

import pytest

from perf_suite import client, operations


class _RunningJobHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"job_id": "job-1", "status": "running", "result": None})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def running_job_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RunningJobHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.timeout(15)
def test_poll_that_stays_running_ends_in_poll_deadline_error(running_job_url: str):
    perf = client.PerfClient(running_job_url, "example-user", "example-pass")
    perf._token = "example-token"
    perf._token_obtained_at = float("inf")
    with pytest.raises(client.RequestFailedError, match="poll deadline"):
        operations.poll_xray(perf, "rest", "job-1", deadline_s=0.3)
    assert perf.tally()["GET /api/jobs/{job_id}"] >= 2


@pytest.mark.parametrize(
    "deadline", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0]
)
def test_poll_xray_rejects_non_finite_or_non_positive_deadline(
    running_job_url: str, deadline: float
):
    perf = client.PerfClient(running_job_url, "example-user", "example-pass")
    perf._token = "example-token"
    perf._token_obtained_at = float("inf")
    with pytest.raises(ValueError, match="deadline"):
        operations.poll_xray(perf, "rest", "job-1", deadline_s=deadline)
    assert perf.tally() == {}


def test_default_deadline_is_server_job_timeout_plus_margin():
    assert operations.SERVER_XRAY_JOB_TIMEOUT_S == 120
    assert operations.DEFAULT_XRAY_POLL_DEADLINE_S == 120 + 180
