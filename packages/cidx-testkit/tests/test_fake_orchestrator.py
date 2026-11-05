"""The fake orchestrator answers the routes the CLI invoker delegate uses."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, Optional, Tuple

import pytest

from cidx_testkit.fakes.orchestrator import FAKE_PASSWORD, FakeOrchestrator


@pytest.fixture
def service() -> Iterator[FakeOrchestrator]:
    with FakeOrchestrator() as fake:
        yield fake


def _call(
    service: FakeOrchestrator,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
) -> Tuple[int, Dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    method = "POST" if body is not None else "GET"
    request = urllib.request.Request(
        service.url + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _login(service: FakeOrchestrator) -> str:
    status, payload = _call(
        service, "/auth/login", {"username": "example", "password": FAKE_PASSWORD}
    )
    assert status == 200
    return str(payload["token"])


def test_login_issues_a_token_and_refuses_a_wrong_password(
    service: FakeOrchestrator,
) -> None:
    assert _login(service)
    status, _ = _call(service, "/auth/login", {"password": "wrong"})
    assert status == 401


def test_a_job_runs_then_completes_with_the_configured_output(
    service: FakeOrchestrator,
) -> None:
    service.succeed_with("done")
    token = _login(service)
    status, submitted = _call(
        service, "/jobs/with-capabilities", {"jobId": "job-1", "prompt": "p"}, token
    )
    assert status == 201 and submitted == {"jobId": "job-1"}
    assert _call(service, "/jobs/job-1", token=token)[1]["status"] == "running"
    _, final = _call(service, "/jobs/job-1", token=token)
    assert final["status"] == "completed"
    assert final["result"] == {"status": "completed", "exitCode": 0, "output": "done"}
    assert service.submitted == [{"jobId": "job-1", "prompt": "p"}]


def test_a_failed_job_carries_its_error(service: FakeOrchestrator) -> None:
    service.polls_before_terminal = 0
    service.fail_with("boom")
    token = _login(service)
    _call(service, "/jobs/with-capabilities", {"jobId": "j"}, token)
    _, final = _call(service, "/jobs/j", token=token)
    assert final["status"] == "failed" and final["result"]["error"] == "boom"


def test_capacity_rejections_answer_429_with_a_retry_hint(
    service: FakeOrchestrator,
) -> None:
    service.capacity_rejections = 1
    service.retry_after_seconds = 3
    token = _login(service)
    assert _call(service, "/jobs/with-capabilities", {"jobId": "a"}, token) == (
        429,
        {"retryAfterSeconds": 3},
    )
    assert _call(service, "/jobs/with-capabilities", {"jobId": "a"}, token)[0] == 201


def test_a_revoked_or_missing_token_is_401(service: FakeOrchestrator) -> None:
    token = _login(service)
    service.revoke_tokens()
    assert _call(service, "/jobs/with-capabilities", {"jobId": "a"}, token)[0] == 401
    assert _call(service, "/jobs/a")[0] == 401


def test_a_service_that_went_away_refuses_connections() -> None:
    service = FakeOrchestrator().start()
    url = service.url
    service.go_away()
    assert service.url == url
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(url + "/auth/login", timeout=5)
