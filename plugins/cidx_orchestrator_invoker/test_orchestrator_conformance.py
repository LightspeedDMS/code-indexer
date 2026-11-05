"""The orchestrator invoker passes the CLI invoker seam's conformance suite.

Run against the testkit's fake orchestrator, a real HTTP server on the
loopback interface: the delegate is exercised over the wire exactly as
deployed, with nothing patched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

import cidx_orchestrator_invoker as plugin
from cidx_testkit.conformance.cli_invoker import (
    CliInvokerContract,
    CliInvokerSubject,
)
from cidx_testkit.fakes.orchestrator import (
    FAKE_PASSWORD,
    FakeOrchestrator,
    orchestrator_subject,
)


@pytest.fixture
def fake_orchestrator(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeOrchestrator]:
    with FakeOrchestrator() as service:
        monkeypatch.setenv("CIDX_ORCH_URL", service.url)
        monkeypatch.setenv("CIDX_ORCH_PASSWORD", FAKE_PASSWORD)
        monkeypatch.setenv("CIDX_ORCH_POLL_SECS", "0")
        monkeypatch.delenv("CIDX_ORCH_MODEL", raising=False)
        yield service


class TestOrchestratorInvoker(CliInvokerContract):
    @pytest.fixture
    def cli_subject(
        self, fake_orchestrator: FakeOrchestrator, tmp_path: Path
    ) -> CliInvokerSubject:
        return orchestrator_subject(
            plugin.make_invoker(), fake_orchestrator, str(tmp_path)
        )


def test_capacity_is_waited_out_and_the_job_still_runs(
    fake_orchestrator: FakeOrchestrator, tmp_path: Path
) -> None:
    fake_orchestrator.capacity_rejections = 2
    fake_orchestrator.succeed_with("ok")
    result = plugin.make_invoker().invoke("describe", str(tmp_path), "p", 60)
    assert result.success, result.error
    assert len(fake_orchestrator.submitted) == 1


def test_a_revoked_token_forces_one_reauthentication(
    fake_orchestrator: FakeOrchestrator, tmp_path: Path
) -> None:
    invoker = plugin.make_invoker()
    fake_orchestrator.succeed_with("first")
    assert invoker.invoke("describe", str(tmp_path), "p", 60).success
    fake_orchestrator.revoke_tokens()
    fake_orchestrator.succeed_with("second")
    result = invoker.invoke("describe", str(tmp_path), "p", 60)
    assert result.success and result.output == "second"
    logins = [r for r in fake_orchestrator.requests if r[1] == "/auth/login"]
    assert len(logins) == 2
