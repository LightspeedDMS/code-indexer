"""Conformance suite for the CLI invoker seam (``cidx_sdk.seams.cli_invoker``).

Every CLI invoker the core's dispatcher can be handed -- each delegate, and
any local one testable without a real LLM CLI -- must pass the same checks.
Subclass :class:`CliInvokerContract` and implement the ``cli_subject``
fixture, returning a :class:`CliInvokerSubject`: the invoker under test plus
the controls that make the service behind it answer a run a given way::

    class TestOrchestratorInvoker(CliInvokerContract):
        @pytest.fixture
        def cli_subject(self, fake_orchestrator, monkeypatch):
            monkeypatch.setenv("CIDX_ORCH_URL", fake_orchestrator.url)
            ...
            return orchestrator_subject(make_invoker(), fake_orchestrator)

The contract is what the dispatcher relies on: an :class:`InvocationResult`
for EVERY outcome (a failure is a result with a ``failure_class``, never an
exception -- the dispatcher decides retry and failover from it), the run's
output on success, and the caller's timeout honoured.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

import pytest

from cidx_sdk.seams.cli_invoker import CliInvoker, FailureClass, InvocationResult

#: A flow name and prompt the checks send (neutral sample data).
SAMPLE_FLOW = "describe"
SAMPLE_PROMPT = "Describe the example repository."


@dataclass
class CliInvokerSubject:
    """An invoker under test and the controls of the service behind it.

    Attributes:
        invoker: the implementation under test.
        cwd: the working directory handed to ``invoke``.
        succeed: ``succeed(output)`` -- the next run completes with *output*.
        fail: ``fail(error)`` -- the next run ends in a failure naming *error*.
        unreachable: make the service unreachable from now on.
        hang: the next run never finishes (None: not controllable; the
            timeout check is skipped).
    """

    invoker: CliInvoker
    cwd: str
    succeed: Callable[[str], None]
    fail: Callable[[str], None]
    unreachable: Callable[[], None]
    hang: Optional[Callable[[], None]] = None


class CliInvokerContract:
    """The CLI invoker's contract. Subclass and provide ``cli_subject``."""

    #: Seconds a run that never finishes is given before it must come back.
    timeout_seconds = 2
    #: Slack allowed past the timeout (connection setup, one poll).
    timeout_slack_seconds = 5.0

    @pytest.fixture
    def cli_subject(self) -> CliInvokerSubject:
        raise NotImplementedError("provide the invoker under test")

    def _invoke(self, subject: CliInvokerSubject, timeout: int = 60) -> object:
        return subject.invoker.invoke(SAMPLE_FLOW, subject.cwd, SAMPLE_PROMPT, timeout)

    def test_satisfies_the_protocol(self, cli_subject: CliInvokerSubject) -> None:
        assert isinstance(cli_subject.invoker, CliInvoker)

    def test_a_completed_run_returns_its_output(
        self, cli_subject: CliInvokerSubject
    ) -> None:
        cli_subject.succeed('{"description": "An example service."}')
        result = self._invoke(cli_subject)
        assert isinstance(result, InvocationResult)
        assert result.success is True
        assert result.output == '{"description": "An example service."}'
        assert result.error == ""
        assert result.failure_class is None
        assert result.cli_used

    def test_a_failed_run_is_a_classified_result_not_an_exception(
        self, cli_subject: CliInvokerSubject
    ) -> None:
        cli_subject.fail("the example run failed")
        result = self._invoke(cli_subject)
        assert isinstance(result, InvocationResult)
        assert result.success is False
        assert result.output == ""
        assert "the example run failed" in result.error
        assert isinstance(result.failure_class, FailureClass)

    def test_an_unreachable_service_is_a_classified_result(
        self, cli_subject: CliInvokerSubject
    ) -> None:
        cli_subject.unreachable()
        result = self._invoke(cli_subject)
        assert isinstance(result, InvocationResult)
        assert result.success is False
        assert result.error
        assert isinstance(result.failure_class, FailureClass)

    def test_a_run_that_never_finishes_comes_back_after_the_timeout(
        self, cli_subject: CliInvokerSubject
    ) -> None:
        if cli_subject.hang is None:
            pytest.skip("this subject cannot make a run hang")
        cli_subject.hang()
        started = time.monotonic()
        result = self._invoke(cli_subject, timeout=self.timeout_seconds)
        elapsed = time.monotonic() - started
        assert isinstance(result, InvocationResult)
        assert result.success is False
        assert isinstance(result.failure_class, FailureClass)
        assert elapsed < self.timeout_seconds + self.timeout_slack_seconds
