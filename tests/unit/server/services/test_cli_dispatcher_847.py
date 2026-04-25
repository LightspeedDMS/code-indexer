"""
Tests for CliDispatcher (Story #847).

CliDispatcher selects between Claude and Codex invokers based on a weight,
retries RETRYABLE_ON_SAME failures once on the same invoker, and fails over
to the alternate invoker for all other failures.

All invoker calls use stub IntelligenceCliInvoker implementations (no subprocess).
random.random is mocked deterministically where needed to eliminate flakiness.
"""

from __future__ import annotations

import itertools
import os
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    InvocationResult,
)

# ---------------------------------------------------------------------------
# Named constants — all configuration values named; no inline magic numbers
# in executable code.  Call-count assertions (0, 1, 2) are self-documenting
# structural counts and are acceptable as literals per project convention.
# ---------------------------------------------------------------------------

_CWD = os.devnull               # portable, guaranteed-present path on Unix
_FLOW = "describe"
_PROMPT = "describe this repo"
_TIMEOUT_S = 30                  # hard timeout passed to dispatch()

_SMALL_DISPATCH_COUNT = 100      # loops for codex-disabled scenarios
_TINY_DISPATCH_COUNT = 10        # smallest loop used in one targeted test
_LARGE_DISPATCH_COUNT = 1000     # statistical distribution test

_CODEX_WEIGHT_NONE = 0.0         # all dispatches go to Claude
_CODEX_WEIGHT_ALL = 1.0          # all dispatches go to Codex
_CODEX_WEIGHT_HALF = 0.5         # fifty/fifty split
_NON_ZERO_CODEX_WEIGHT = 0.9     # non-zero weight used with codex=None

_BELOW_ZERO_WEIGHT = -0.01       # invalid: below lower bound
_ABOVE_ONE_WEIGHT = 1.01         # invalid: above upper bound

# Deterministic random values for distribution tests.
# _RANDOM_LOW < _CODEX_WEIGHT_HALF  → Codex selected as primary.
# _RANDOM_HIGH >= _CODEX_WEIGHT_HALF → Claude selected as primary.
_RANDOM_LOW = 0.3
_RANDOM_HIGH = 0.7

# Statistical tolerance for weight=_CODEX_WEIGHT_HALF over _LARGE_DISPATCH_COUNT
_LOWER_BOUND = 400
_UPPER_BOUND = 600


# ---------------------------------------------------------------------------
# Stub invoker helpers
# ---------------------------------------------------------------------------


def _success_result(cli_used: str) -> InvocationResult:
    return InvocationResult(
        success=True,
        output=f"output from {cli_used}",
        error="",
        cli_used=cli_used,
        was_failover=False,
    )


def _failure_result(cli_used: str, failure_class: FailureClass) -> InvocationResult:
    return InvocationResult(
        success=False,
        output="",
        error=f"error from {cli_used}",
        cli_used=cli_used,
        was_failover=False,
        failure_class=failure_class,
    )


def _stub_invoker(results: list) -> MagicMock:
    """Build a MagicMock satisfying IntelligenceCliInvoker protocol.

    invoke() returns results in sequence (raises StopIteration if exhausted).
    """
    stub = MagicMock(spec=["invoke"])
    stub.invoke = MagicMock(side_effect=results)
    return stub


def _dispatch(dispatcher: "CliDispatcher") -> InvocationResult:
    """Call dispatcher.dispatch with shared test constants."""
    return dispatcher.dispatch(
        flow=_FLOW, cwd=_CWD, prompt=_PROMPT, timeout=_TIMEOUT_S
    )


def _make_dispatcher(
    claude_results: list,
    codex_results: list | None = None,
    codex_weight: float = _CODEX_WEIGHT_HALF,
) -> "tuple":
    """Return (dispatcher, claude_stub, codex_stub) with pre-wired stubs."""
    from code_indexer.server.services.cli_dispatcher import CliDispatcher

    claude_stub = _stub_invoker(claude_results)
    if codex_results is None:
        return CliDispatcher(claude=claude_stub, codex=None), claude_stub, None
    codex_stub = _stub_invoker(codex_results)
    dispatcher = CliDispatcher(
        claude=claude_stub, codex=codex_stub, codex_weight=codex_weight
    )
    return dispatcher, claude_stub, codex_stub


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestCliDispatcherConstructor:
    def test_codex_weight_below_zero_raises_value_error(self):
        """codex_weight below _BELOW_ZERO_WEIGHT raises ValueError."""
        from code_indexer.server.services.cli_dispatcher import CliDispatcher

        claude_stub = _stub_invoker([])
        with pytest.raises(ValueError, match="codex_weight"):
            CliDispatcher(
                claude=claude_stub, codex=None, codex_weight=_BELOW_ZERO_WEIGHT
            )

    def test_codex_weight_above_one_raises_value_error(self):
        """codex_weight above _ABOVE_ONE_WEIGHT raises ValueError."""
        from code_indexer.server.services.cli_dispatcher import CliDispatcher

        claude_stub = _stub_invoker([])
        with pytest.raises(ValueError, match="codex_weight"):
            CliDispatcher(
                claude=claude_stub, codex=None, codex_weight=_ABOVE_ONE_WEIGHT
            )

    def test_codex_weight_zero_is_valid(self):
        """codex_weight == _CODEX_WEIGHT_NONE is accepted."""
        from code_indexer.server.services.cli_dispatcher import CliDispatcher

        claude_stub = _stub_invoker([])
        dispatcher = CliDispatcher(
            claude=claude_stub, codex=None, codex_weight=_CODEX_WEIGHT_NONE
        )
        assert dispatcher is not None

    def test_codex_weight_one_is_valid(self):
        """codex_weight == _CODEX_WEIGHT_ALL is accepted."""
        from code_indexer.server.services.cli_dispatcher import CliDispatcher

        claude_stub = _stub_invoker([])
        codex_stub = _stub_invoker([])
        dispatcher = CliDispatcher(
            claude=claude_stub, codex=codex_stub, codex_weight=_CODEX_WEIGHT_ALL
        )
        assert dispatcher is not None


# ---------------------------------------------------------------------------
# Codex disabled (codex=None)
# ---------------------------------------------------------------------------


class TestCliDispatcherCodexDisabled:
    def test_codex_none_all_dispatches_go_to_claude(self):
        """When codex is None, all _SMALL_DISPATCH_COUNT dispatches use Claude."""
        from code_indexer.server.services.cli_dispatcher import CliDispatcher

        claude_stub = _stub_invoker(
            [_success_result("claude")] * _SMALL_DISPATCH_COUNT
        )
        dispatcher = CliDispatcher(claude=claude_stub, codex=None)
        for _ in range(_SMALL_DISPATCH_COUNT):
            result = _dispatch(dispatcher)
            assert result.cli_used == "claude"
        assert claude_stub.invoke.call_count == _SMALL_DISPATCH_COUNT

    def test_codex_none_effective_weight_is_zero(self):
        """When codex=None, _NON_ZERO_CODEX_WEIGHT kwarg is ignored — Claude always called."""
        from code_indexer.server.services.cli_dispatcher import CliDispatcher

        claude_stub = _stub_invoker(
            [_success_result("claude")] * _TINY_DISPATCH_COUNT
        )
        dispatcher = CliDispatcher(
            claude=claude_stub, codex=None, codex_weight=_NON_ZERO_CODEX_WEIGHT
        )
        for _ in range(_TINY_DISPATCH_COUNT):
            _dispatch(dispatcher)
        assert claude_stub.invoke.call_count == _TINY_DISPATCH_COUNT


# ---------------------------------------------------------------------------
# Weight-based primary selection
# ---------------------------------------------------------------------------


class TestCliDispatcherWeightSelection:
    def test_codex_weight_zero_all_primary_calls_go_to_claude(self):
        """_CODEX_WEIGHT_NONE: Claude is always primary; Codex never called."""
        dispatcher, claude_stub, codex_stub = _make_dispatcher(
            claude_results=[_success_result("claude")] * _LARGE_DISPATCH_COUNT,
            codex_results=[_success_result("codex")] * _LARGE_DISPATCH_COUNT,
            codex_weight=_CODEX_WEIGHT_NONE,
        )
        side_effects = [
            i / _LARGE_DISPATCH_COUNT for i in range(_LARGE_DISPATCH_COUNT)
        ]
        with patch("random.random", side_effect=side_effects):
            for _ in range(_LARGE_DISPATCH_COUNT):
                _dispatch(dispatcher)
        assert claude_stub.invoke.call_count == _LARGE_DISPATCH_COUNT
        assert codex_stub.invoke.call_count == 0

    def test_codex_weight_one_all_primary_calls_go_to_codex(self):
        """_CODEX_WEIGHT_ALL: Codex is always primary; Claude never called."""
        dispatcher, claude_stub, codex_stub = _make_dispatcher(
            claude_results=[_success_result("claude")] * _LARGE_DISPATCH_COUNT,
            codex_results=[_success_result("codex")] * _LARGE_DISPATCH_COUNT,
            codex_weight=_CODEX_WEIGHT_ALL,
        )
        side_effects = [
            i / _LARGE_DISPATCH_COUNT for i in range(_LARGE_DISPATCH_COUNT)
        ]
        with patch("random.random", side_effect=side_effects):
            for _ in range(_LARGE_DISPATCH_COUNT):
                _dispatch(dispatcher)
        assert codex_stub.invoke.call_count == _LARGE_DISPATCH_COUNT
        assert claude_stub.invoke.call_count == 0

    def test_codex_weight_half_distribution_within_tolerance(self):
        """_CODEX_WEIGHT_HALF: alternating _RANDOM_LOW/_RANDOM_HIGH yields ~_LOWER_BOUND–_UPPER_BOUND Codex calls."""
        dispatcher, claude_stub, codex_stub = _make_dispatcher(
            claude_results=[_success_result("claude")] * _LARGE_DISPATCH_COUNT,
            codex_results=[_success_result("codex")] * _LARGE_DISPATCH_COUNT,
            codex_weight=_CODEX_WEIGHT_HALF,
        )
        # Deterministic alternating sequence: _RANDOM_LOW selects Codex, _RANDOM_HIGH selects Claude
        side_effects = list(
            itertools.islice(
                itertools.cycle([_RANDOM_LOW, _RANDOM_HIGH]),
                _LARGE_DISPATCH_COUNT,
            )
        )
        with patch("random.random", side_effect=side_effects):
            for _ in range(_LARGE_DISPATCH_COUNT):
                _dispatch(dispatcher)
        codex_calls = codex_stub.invoke.call_count
        assert _LOWER_BOUND <= codex_calls <= _UPPER_BOUND, (
            f"Expected {_LOWER_BOUND}–{_UPPER_BOUND} codex calls at "
            f"weight={_CODEX_WEIGHT_HALF}, got {codex_calls}"
        )


# ---------------------------------------------------------------------------
# Failover logic
# ---------------------------------------------------------------------------


class TestCliDispatcherFailover:
    def test_primary_retryable_on_other_triggers_failover(self):
        """Primary RETRYABLE_ON_OTHER → alternate called once; was_failover=True."""
        dispatcher, claude_stub, codex_stub = _make_dispatcher(
            claude_results=[_failure_result("claude", FailureClass.RETRYABLE_ON_OTHER)],
            codex_results=[_success_result("codex")],
            codex_weight=_CODEX_WEIGHT_NONE,  # Claude always primary
        )
        result = _dispatch(dispatcher)
        assert result.was_failover is True
        assert result.success is True
        assert claude_stub.invoke.call_count == 1
        assert codex_stub.invoke.call_count == 1

    def test_primary_success_alternate_never_called(self):
        """Primary success → alternate is never called."""
        dispatcher, claude_stub, codex_stub = _make_dispatcher(
            claude_results=[_success_result("claude")],
            codex_results=[],
            codex_weight=_CODEX_WEIGHT_NONE,  # Claude always primary
        )
        result = _dispatch(dispatcher)
        assert result.success is True
        assert claude_stub.invoke.call_count == 1
        assert codex_stub.invoke.call_count == 0


# ---------------------------------------------------------------------------
# Retry on RETRYABLE_ON_SAME
# ---------------------------------------------------------------------------


class TestCliDispatcherRetry:
    def test_retryable_on_same_retry_succeeds_no_failover(self):
        """Primary RETRYABLE_ON_SAME → retry succeeds; was_failover=False; primary called twice."""
        dispatcher, claude_stub, codex_stub = _make_dispatcher(
            claude_results=[
                _failure_result("claude", FailureClass.RETRYABLE_ON_SAME),
                _success_result("claude"),
            ],
            codex_results=[_success_result("codex")],
            codex_weight=_CODEX_WEIGHT_NONE,  # Claude always primary
        )
        result = _dispatch(dispatcher)
        assert result.success is True
        assert result.was_failover is False
        assert claude_stub.invoke.call_count == 2
        assert codex_stub.invoke.call_count == 0

    def test_retryable_on_same_retry_fails_then_failover(self):
        """Primary RETRYABLE_ON_SAME twice → failover to alternate; was_failover=True."""
        dispatcher, claude_stub, codex_stub = _make_dispatcher(
            claude_results=[
                _failure_result("claude", FailureClass.RETRYABLE_ON_SAME),
                _failure_result("claude", FailureClass.RETRYABLE_ON_SAME),
            ],
            codex_results=[_success_result("codex")],
            codex_weight=_CODEX_WEIGHT_NONE,  # Claude always primary
        )
        result = _dispatch(dispatcher)
        assert result.was_failover is True
        assert result.success is True
        assert claude_stub.invoke.call_count == 2
        assert codex_stub.invoke.call_count == 1


# ---------------------------------------------------------------------------
# Both fail
# ---------------------------------------------------------------------------


class TestCliDispatcherBothFail:
    def test_both_fail_error_contains_both_invoker_errors(self):
        """Primary and alternate both fail → result.error mentions both CLIs."""
        dispatcher, claude_stub, codex_stub = _make_dispatcher(
            claude_results=[_failure_result("claude", FailureClass.RETRYABLE_ON_OTHER)],
            codex_results=[_failure_result("codex", FailureClass.RETRYABLE_ON_OTHER)],
            codex_weight=_CODEX_WEIGHT_NONE,  # Claude always primary
        )
        result = _dispatch(dispatcher)
        assert result.success is False
        assert result.was_failover is True
        assert "claude" in result.error
        assert "codex" in result.error

    def test_both_fail_result_cli_used_is_alternate(self):
        """When both fail, result.cli_used is the alternate's cli_used."""
        dispatcher, claude_stub, codex_stub = _make_dispatcher(
            claude_results=[_failure_result("claude", FailureClass.RETRYABLE_ON_OTHER)],
            codex_results=[_failure_result("codex", FailureClass.RETRYABLE_ON_OTHER)],
            codex_weight=_CODEX_WEIGHT_NONE,  # Claude always primary
        )
        result = _dispatch(dispatcher)
        assert result.cli_used == "codex"
