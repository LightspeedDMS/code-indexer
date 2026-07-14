"""
Tests for the orchestrator-backed LLM invoker.

The contract cidx relies on is narrow but unforgiving: return (ok, output), and
NEVER raise. This plugin runs inside a cidx background job (golden-repo
registration); an escaping exception fails that job, which is the exact failure
mode -- exit 127 from a missing `claude` CLI -- that the plugin exists to cure.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import cidx_orchestrator_invoker as plugin


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CIDX_ORCH_PASSWORD", "secret")
    monkeypatch.setenv("CIDX_ORCH_POLL_SECS", "0")
    plugin._token = None
    yield
    plugin._token = None


def _http_error(code: int, body: dict | None = None) -> urllib.error.HTTPError:
    payload = json.dumps(body or {}).encode()
    return urllib.error.HTTPError(
        url="http://orch/jobs", code=code, msg="err", hdrs=None, fp=io.BytesIO(payload)
    )


class TestHappyPath:
    def test_submits_polls_and_returns_the_agent_output(self, monkeypatch):
        seen = []

        def fake_request(path, *, method="GET", body=None, token=None, timeout=30):
            seen.append((method, path))
            if path == "/auth/login":
                return {"token": "jwt-123"}
            if path == "/jobs/with-capabilities":
                assert body["prompt"] == "analyze the repo"
                assert body["options"]["agentEngine"] == "claude"
                return {"jobId": "job-1"}
            if path == "/jobs/job-1":
                return {
                    "status": "completed",
                    "result": {
                        "status": "completed",
                        "exitCode": 0,
                        "output": "THE DESCRIPTION",
                    },
                }
            raise AssertionError(path)

        monkeypatch.setattr(plugin, "_request", fake_request)

        ok, out = plugin.invoke("/repo", "analyze the repo", 300, 360)

        assert (ok, out) == (True, "THE DESCRIPTION")
        assert ("POST", "/jobs/with-capabilities") in seen
        assert ("GET", "/jobs/job-1") in seen

    def test_polls_until_terminal(self, monkeypatch):
        states = iter(
            [
                {"status": "queued"},
                {"status": "running"},
                {
                    "status": "completed",
                    "result": {"status": "completed", "exitCode": 0, "output": "done"},
                },
            ]
        )

        def fake_request(path, *, method="GET", body=None, token=None, timeout=30):
            if path == "/auth/login":
                return {"token": "t"}
            if path == "/jobs/with-capabilities":
                return {"jobId": "j"}
            return next(states)

        monkeypatch.setattr(plugin, "_request", fake_request)

        assert plugin.invoke("/repo", "p", 300, 360) == (True, "done")


class TestFailuresNeverEscape:
    """Every failure must come back as (False, message) -- never an exception."""

    def test_agent_job_failed(self, monkeypatch):
        def fake_request(path, *, method="GET", body=None, token=None, timeout=30):
            if path == "/auth/login":
                return {"token": "t"}
            if path == "/jobs/with-capabilities":
                return {"jobId": "j"}
            return {
                "status": "failed",
                "result": {"status": "failed", "exitCode": 1, "error": "agent blew up"},
            }

        monkeypatch.setattr(plugin, "_request", fake_request)

        ok, out = plugin.invoke("/repo", "p", 300, 360)
        assert ok is False
        assert "agent blew up" in out

    def test_nonzero_exit_is_a_failure_even_if_status_says_completed(self, monkeypatch):
        def fake_request(path, *, method="GET", body=None, token=None, timeout=30):
            if path == "/auth/login":
                return {"token": "t"}
            if path == "/jobs/with-capabilities":
                return {"jobId": "j"}
            return {
                "status": "completed",
                "result": {"status": "completed", "exitCode": 3, "output": "partial"},
            }

        monkeypatch.setattr(plugin, "_request", fake_request)

        ok, _ = plugin.invoke("/repo", "p", 300, 360)
        assert ok is False

    def test_missing_password_is_reported_not_raised(self, monkeypatch):
        monkeypatch.delenv("CIDX_ORCH_PASSWORD", raising=False)

        ok, out = plugin.invoke("/repo", "p", 300, 360)

        assert ok is False
        assert "CIDX_ORCH_PASSWORD" in out

    def test_unexpected_crash_is_reported_not_raised(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("socket exploded")

        monkeypatch.setattr(plugin, "_request", boom)

        ok, out = plugin.invoke("/repo", "p", 300, 360)

        assert ok is False
        assert "socket exploded" in out


class TestBackpressureAndAuth:
    def test_429_is_retried_after_the_hinted_delay(self, monkeypatch):
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

        monkeypatch.setattr(plugin, "_request", fake_request)

        ok, out = plugin.invoke("/repo", "p", 300, 360)

        assert (ok, out) == (True, "ok")
        assert calls["submit"] == 2, "the submit must be retried, not abandoned"
        assert 7 in slept, "must honour the orchestrator's retryAfterSeconds hint"

    def test_expired_jwt_triggers_one_reauth_then_succeeds(self, monkeypatch):
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

        monkeypatch.setattr(plugin, "_request", fake_request)

        ok, _ = plugin.invoke("/repo", "p", 300, 360)

        assert ok is True
        assert calls["login"] >= 2, "a 401 must force a re-login"

    def test_token_is_cached_across_calls(self, monkeypatch):
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

        monkeypatch.setattr(plugin, "_request", fake_request)

        plugin.invoke("/repo", "p", 300, 360)
        plugin.invoke("/repo", "p", 300, 360)

        assert calls["login"] == 1, "the 8h JWT must not be re-fetched on every call"
