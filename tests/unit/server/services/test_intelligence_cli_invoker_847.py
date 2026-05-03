"""
Unit tests for IntelligenceCliInvoker protocol, InvocationResult dataclass,
and FailureClass enum (Story #847).

Covers:
- InvocationResult defaults and field values
- InvocationResult serialization via dataclasses.asdict
- FailureClass enum members
- IntelligenceCliInvoker protocol duck-type compliance via isinstance checks
  (requires @runtime_checkable on the Protocol)
"""

from __future__ import annotations

import dataclasses

from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    InvocationResult,
    IntelligenceCliInvoker,
)


# ---------------------------------------------------------------------------
# FailureClass enum tests
# ---------------------------------------------------------------------------


class TestFailureClass:
    def test_retryable_on_same_member_exists(self) -> None:
        assert FailureClass.RETRYABLE_ON_SAME.value == "retryable_on_same"

    def test_retryable_on_other_member_exists(self) -> None:
        assert FailureClass.RETRYABLE_ON_OTHER.value == "retryable_on_other"

    def test_exactly_two_members(self) -> None:
        members = list(FailureClass)
        assert len(members) == 2

    def test_members_are_distinct(self) -> None:
        assert FailureClass.RETRYABLE_ON_SAME != FailureClass.RETRYABLE_ON_OTHER


# ---------------------------------------------------------------------------
# InvocationResult dataclass tests
# ---------------------------------------------------------------------------


class TestInvocationResult:
    def test_success_result_fields(self) -> None:
        result = InvocationResult(
            success=True,
            output="some output",
            error="",
            cli_used="claude",
            was_failover=False,
        )
        assert result.success is True
        assert result.output == "some output"
        assert result.error == ""
        assert result.cli_used == "claude"
        assert result.was_failover is False

    def test_failure_class_defaults_to_none(self) -> None:
        result = InvocationResult(
            success=True,
            output="",
            error="",
            cli_used="claude",
            was_failover=False,
        )
        assert result.failure_class is None

    def test_failure_result_with_failure_class(self) -> None:
        result = InvocationResult(
            success=False,
            output="",
            error="timed out",
            cli_used="codex",
            was_failover=False,
            failure_class=FailureClass.RETRYABLE_ON_SAME,
        )
        assert result.success is False
        assert result.failure_class == FailureClass.RETRYABLE_ON_SAME
        assert result.cli_used == "codex"

    def test_was_failover_can_be_set_true(self) -> None:
        result = InvocationResult(
            success=True,
            output="description text",
            error="",
            cli_used="codex",
            was_failover=True,
        )
        assert result.was_failover is True

    def test_retryable_on_other_failure_class(self) -> None:
        result = InvocationResult(
            success=False,
            output="",
            error="auth error",
            cli_used="claude",
            was_failover=False,
            failure_class=FailureClass.RETRYABLE_ON_OTHER,
        )
        assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER

    def test_serialization_success_result_all_fields_present(self) -> None:
        """dataclasses.asdict includes all six fields for a success result."""
        result = InvocationResult(
            success=True,
            output="great description",
            error="",
            cli_used="claude",
            was_failover=False,
        )
        as_dict = dataclasses.asdict(result)
        assert as_dict["success"] is True
        assert as_dict["output"] == "great description"
        assert as_dict["error"] == ""
        assert as_dict["cli_used"] == "claude"
        assert as_dict["was_failover"] is False
        assert as_dict["failure_class"] is None

    def test_serialization_preserves_failure_class_enum_object(self) -> None:
        """dataclasses.asdict preserves the FailureClass enum object (not auto-converted to str)."""
        result = InvocationResult(
            success=False,
            output="",
            error="quota exceeded",
            cli_used="codex",
            was_failover=True,
            failure_class=FailureClass.RETRYABLE_ON_OTHER,
        )
        as_dict = dataclasses.asdict(result)
        # asdict preserves enum identity; value is accessible via .value
        assert as_dict["failure_class"] == FailureClass.RETRYABLE_ON_OTHER
        assert as_dict["was_failover"] is True
        assert as_dict["cli_used"] == "codex"

    def test_serialization_retryable_on_same_preserved(self) -> None:
        """dataclasses.asdict preserves RETRYABLE_ON_SAME enum identity."""
        result = InvocationResult(
            success=False,
            output="",
            error="timeout",
            cli_used="claude",
            was_failover=False,
            failure_class=FailureClass.RETRYABLE_ON_SAME,
        )
        as_dict = dataclasses.asdict(result)
        assert as_dict["failure_class"] == FailureClass.RETRYABLE_ON_SAME


# ---------------------------------------------------------------------------
# IntelligenceCliInvoker protocol tests (runtime_checkable isinstance)
# ---------------------------------------------------------------------------


class TestIntelligenceCliInvokerProtocol:
    def test_class_with_invoke_method_is_instance_of_protocol(self) -> None:
        """A class that has invoke(flow, cwd, prompt, timeout) passes isinstance check."""

        class StubInvoker:
            def invoke(
                self, flow: str, cwd: str, prompt: str, timeout: int
            ) -> InvocationResult:
                return InvocationResult(
                    success=True,
                    output="stub",
                    error="",
                    cli_used="stub",
                    was_failover=False,
                )

        stub = StubInvoker()
        # Protocol must be @runtime_checkable for this assertion to work
        assert isinstance(stub, IntelligenceCliInvoker)

    def test_class_missing_invoke_is_not_instance_of_protocol(self) -> None:
        """A class without invoke() fails the isinstance check."""

        class BadInvoker:
            def run(self, prompt: str) -> str:
                return prompt

        bad = BadInvoker()
        assert not isinstance(bad, IntelligenceCliInvoker)

    def test_invoke_result_shape_from_protocol_compliant_class(self) -> None:
        """Calling invoke on a protocol-compliant stub returns InvocationResult."""

        class ConcreteStub:
            def invoke(
                self, flow: str, cwd: str, prompt: str, timeout: int
            ) -> InvocationResult:
                return InvocationResult(
                    success=False,
                    output="",
                    error="not implemented",
                    cli_used="none",
                    was_failover=False,
                    failure_class=FailureClass.RETRYABLE_ON_OTHER,
                )

        stub = ConcreteStub()
        assert isinstance(stub, IntelligenceCliInvoker)
        result = stub.invoke(flow="refine", cwd="/repo", prompt="p", timeout=30)
        assert isinstance(result, InvocationResult)
        assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER
