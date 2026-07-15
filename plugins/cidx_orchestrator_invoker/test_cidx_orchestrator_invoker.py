"""
Tests for the orchestrator-backed IntelligenceCliInvoker.

The dispatcher contract is unforgiving: invoke(...) must return an
InvocationResult and NEVER raise. This invoker runs inside a cidx background job
(golden-repo registration, dep-map); an escaping exception fails that job, which
is the exact failure mode -- exit 127 from a missing `claude` CLI -- the plugin
exists to cure. On failure it must classify the failure so the dispatcher's
retry/failover logic behaves.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import cidx_orchestrator_invoker as plugin
from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    InvocationResult,
)


@pytest.fixture()
def invoker(monkeypatch):
    monkeypatch.setenv("CIDX_ORCH_PASSWORD", "secret")
    monkeypatch.setenv("CIDX_ORCH_POLL_SECS", "0")
    return plugin.make_invoker("opus", 300)


def _http_error(code: int, body: dict | None = None) -> urllib.error.HTTPError:
    payload = json.dumps(body or {}).encode()
    return urllib.error.HTTPError(
        url="http://orch", code=code, msg="err", hdrs=None, fp=io.BytesIO(payload)
    )


class TestContract:
    def test_make_invoker_satisfies_the_protocol(self, invoker):
        from code_indexer.server.services.intelligence_cli_invoker import (
            IntelligenceCliInvoker,
        )

        assert isinstance(invoker, IntelligenceCliInvoker)

    def test_model_override_env_wins(self, monkeypatch):
        monkeypatch.setenv("CIDX_ORCH_PASSWORD", "x")
        monkeypatch.setenv("CIDX_ORCH_MODEL", "sonnet")
        inv = plugin.make_invoker("opus", 300)
        assert inv._model == "sonnet"


class TestHappyPath:
    def test_submits_polls_and_returns_output(self, invoker, monkeypatch):
        seen = []

        def fake_request(path, *, method="GET", body=None, token=None, timeout=30):
            seen.append((method, path))
            if path == "/auth/login":
                return {"token": "jwt"}
            if path == "/jobs/with-capabilities":
                assert body["prompt"] == "describe the repo"
                assert body["options"]["model"] == "opus"
                assert body["options"]["outputMode"] == "result"
                return {"jobId": "job-1"}
            return {
                "status": "completed",
                "result": {"status": "completed", "exitCode": 0, "output": "DESC"},
            }

        monkeypatch.setattr(invoker, "_request", fake_request)

        res = invoker.invoke("describe", "/repo", "describe the repo", 300)

        assert isinstance(res, InvocationResult)
        assert res.success is True
        assert res.output == "DESC"
        assert res.cli_used == "orchestrator"
        assert ("POST", "/jobs/with-capabilities") in seen

    def test_polls_until_terminal(self, invoker, monkeypatch):
        states = iter(
            [
                {"status": "queued"},
                {"status": "running"},
                {
                    "status": "completed",
                    "result": {"status": "completed", "exitCode": 0, "output": "ok"},
                },
            ]
        )

        def fake_request(path, *, method="GET", body=None, token=None, timeout=30):
            if path == "/auth/login":
                return {"token": "t"}
            if path == "/jobs/with-capabilities":
                return {"jobId": "j"}
            return next(states)

        monkeypatch.setattr(invoker, "_request", fake_request)
        assert invoker.invoke("f", "/r", "p", 300).success is True


class TestFailuresNeverRaise:
    def test_agent_job_failed_is_retryable_on_other(self, invoker, monkeypatch):
        def fake_request(path, *, method="GET", body=None, token=None, timeout=30):
            if path == "/auth/login":
                return {"token": "t"}
            if path == "/jobs/with-capabilities":
                return {"jobId": "j"}
            return {
                "status": "failed",
                "result": {"status": "failed", "exitCode": 1, "error": "boom"},
            }

        monkeypatch.setattr(invoker, "_request", fake_request)

        res = invoker.invoke("f", "/r", "p", 300)
        assert res.success is False
        assert "boom" in res.error
        assert res.failure_class == FailureClass.RETRYABLE_ON_OTHER

    def test_nonzero_exit_despite_completed_status_is_failure(self, invoker, monkeypatch):
        def fake_request(path, *, method="GET", body=None, token=None, timeout=30):
            if path == "/auth/login":
                return {"token": "t"}
            if path == "/jobs/with-capabilities":
                return {"jobId": "j"}
            return {
                "status": "completed",
                "result": {"status": "completed", "exitCode": 3, "output": "partial"},
            }

        monkeypatch.setattr(invoker, "_request", fake_request)
        assert invoker.invoke("f", "/r", "p", 300).success is False

    def test_missing_password_is_reported_not_raised(self, monkeypatch):
        monkeypatch.delenv("CIDX_ORCH_PASSWORD", raising=False)
        inv = plugin.make_invoker("opus", 300)

        res = inv.invoke("f", "/r", "p", 300)
        assert res.success is False
        assert "CIDX_ORCH_PASSWORD" in res.error
        assert res.failure_class == FailureClass.RETRYABLE_ON_SAME

    def test_unexpected_crash_is_reported_not_raised(self, invoker, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("socket exploded")

        monkeypatch.setattr(invoker, "_request", boom)

        res = invoker.invoke("f", "/r", "p", 300)
        assert res.success is False
        assert "socket exploded" in res.error


class TestBackpressureAndAuth:
    def test_429_is_retried_after_the_hinted_delay(self, invoker, monkeypatch):
        slept = []
        monkeypatch.setattr(plugin.time, "sleep", lambda s: slept.append(s))
        calls = {"submit": 0}

        def fake_request(path, *, method="GET", body=None, token=None, timeout=30):
            if path == "/auth/login":
                return {"token": "t"}
            if path == "/jobs/with-capabilities":
                calls["submit"] += 1
                if calls["submit"] == 1:
                    raise _http_error(429, {"retryAfterSeconds": 7})
                return {"jobId": "j"}
            return {
                "status": "completed",
                "result": {"status": "completed", "exitCode": 0, "output": "ok"},
            }

        monkeypatch.setattr(invoker, "_request", fake_request)

        res = invoker.invoke("f", "/r", "p", 300)
        assert res.success is True
        assert calls["submit"] == 2
        assert 7 in slept

    def test_expired_jwt_forces_one_reauth(self, invoker, monkeypatch):
        calls = {"login": 0, "submit": 0}

        def fake_request(path, *, method="GET", body=None, token=None, timeout=30):
            if path == "/auth/login":
                calls["login"] += 1
                return {"token": f"jwt-{calls['login']}"}
            if path == "/jobs/with-capabilities":
                calls["submit"] += 1
                if calls["submit"] == 1:
                    raise _http_error(401)
                return {"jobId": "j"}
            return {
                "status": "completed",
                "result": {"status": "completed", "exitCode": 0, "output": "ok"},
            }

        monkeypatch.setattr(invoker, "_request", fake_request)

        assert invoker.invoke("f", "/r", "p", 300).success is True
        assert calls["login"] >= 2

    def test_token_cached_across_calls(self, invoker, monkeypatch):
        calls = {"login": 0}

        def fake_request(path, *, method="GET", body=None, token=None, timeout=30):
            if path == "/auth/login":
                calls["login"] += 1
                return {"token": "t"}
            if path == "/jobs/with-capabilities":
                return {"jobId": "j"}
            return {
                "status": "completed",
                "result": {"status": "completed", "exitCode": 0, "output": "ok"},
            }

        monkeypatch.setattr(invoker, "_request", fake_request)
        invoker.invoke("f", "/r", "p", 300)
        invoker.invoke("f", "/r", "p", 300)
        assert calls["login"] == 1
