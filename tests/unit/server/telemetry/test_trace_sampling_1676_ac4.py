"""
TDD tests for Story #1676 AC4: configurable trace sampling.

Covers the TracerProvider sampler wiring in telemetry/manager.py:
  - an explicit ParentBased(TraceIdRatioBased(trace_sample_rate)) sampler is
    passed into every TracerProvider construction (both the export_traces
    enabled branch and the no-op-fallback branch), replacing today's
    unconfigured default sampler resolution
  - the underlying TraceIdRatioBased sampler's documented deterministic
    trace-ID-based decision function, verified against fixed trace IDs at a
    fixed rate (not a flaky statistical N-run test)
  - a rate of 1.0 (the default) produces the same always-sampled behavior
    as before this AC
  - an already-sampled (or already-dropped) parent context is always
    honored by ParentBased regardless of the configured root rate --
    including through code_indexer.server.telemetry.spans.create_span(),
    which needs no special-casing to inherit this

All tests use the real OpenTelemetry SDK -- no mocking of the code under
test, per MESSI Rule #1.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    TraceIdRatioBased,
)
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    set_span_in_context,
)

from code_indexer.server.utils.config_manager import TelemetryConfig

_TRACE_ID_LIMIT = (1 << 64) - 1


@contextmanager
def _telemetry_manager(**config_kwargs):
    """Construct a real, enabled TelemetryManager and guarantee shutdown.
    Deliberately does not override collector_endpoint -- TelemetryConfig's
    own default is never actually dialed by these tests (TelemetryManager
    only configures the exporter; nothing here awaits an export cycle)."""
    from code_indexer.server.telemetry import TelemetryManager

    config_kwargs.setdefault("enabled", True)
    config = TelemetryConfig(**config_kwargs)
    manager = TelemetryManager(config)
    try:
        yield manager
    finally:
        manager.shutdown()


def _parent_context(sampled: bool):
    """A Context carrying a remote parent SpanContext with the given
    sampled flag -- used to prove ParentBased honors an already-decided
    parent regardless of the configured root sampler's rate."""
    span_context = SpanContext(
        trace_id=0x1234567890ABCDEF1234567890ABCDEF,
        span_id=0x1234567890ABCDEF,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else TraceFlags.DEFAULT),
    )
    return set_span_in_context(NonRecordingSpan(span_context))


class TestSamplerWiringEnabledBranch:
    """AC4: export_traces=True branch passes an explicit sampler.

    Bug #1744 (round 3): deliberately left real (not ported to
    export_traces=False). Paired 1:1 with TestSamplerWiringNoOpBranch
    below at a DIFFERENT trace_sample_rate (0.1 vs 0.25) specifically to
    independently verify the sampler wiring in BOTH the export_traces
    True and False branches of manager.py -- collapsing this test's
    config to match the NoOp branch would make both tests redundant
    duplicates and silently lose regression coverage for a future
    True-branch-specific divergence, with no test failure to signal it.
    Confirmed 6.29s solo cost -- a real, deliberately accepted network
    dependency.
    """

    def test_tracer_provider_sampler_is_parent_based_trace_id_ratio(self):
        with _telemetry_manager(export_traces=True, trace_sample_rate=0.1) as manager:
            sampler = manager.tracer_provider.sampler  # type: ignore[attr-defined]
            assert isinstance(sampler, ParentBased)
            assert "TraceIdRatioBased{0.1}" in sampler.get_description()


class TestSamplerWiringNoOpBranch:
    """AC4: export_traces=False (no-op fallback) branch also passes an
    explicit sampler, for symmetry with the enabled branch."""

    def test_noop_tracer_provider_sampler_is_parent_based_trace_id_ratio(self):
        with _telemetry_manager(export_traces=False, trace_sample_rate=0.25) as manager:
            sampler = manager.tracer_provider.sampler  # type: ignore[attr-defined]
            assert isinstance(sampler, ParentBased)
            assert "TraceIdRatioBased{0.25}" in sampler.get_description()


class TestDefaultRateAlwaysSamples:
    """AC4: trace_sample_rate=1.0 (default) preserves always-on sampling
    for operators who never touch this setting."""

    def test_default_rate_samples_a_root_span(self):
        # Bug #1744: export_traces=False here (not True) is deliberate.
        # The assertions below test the SAMPLER's decision (sampled /
        # is_recording), which manager.py wires identically regardless of
        # this flag (same shared sampler= construction argument in both
        # branches) -- unlike TestSamplerWiringEnabledBranch above, this
        # test has no paired False-branch sibling, so nothing is lost by
        # removing the real, unreachable-network exporter (confirmed
        # 7.88s solo cost before this fix).
        with _telemetry_manager(export_traces=False) as manager:
            tracer = manager.tracer_provider.get_tracer("test")  # type: ignore[attr-defined]
            span = tracer.start_span("root")
            try:
                assert span.get_span_context().trace_flags.sampled is True
                assert span.is_recording() is True
            finally:
                span.end()


class TestDeterministicSamplerContractBoundary:
    """AC4: TraceIdRatioBased's documented decision function -- decision is
    SAMPLE iff (trace_id & TRACE_ID_LIMIT) < round(rate * 2**64) -- verified
    against a fixed set of trace IDs at a fixed rate. Deterministic, not
    statistical. Rate 0.5 is exactly representable in binary floating
    point, so the boundary values below are unambiguous."""

    _RATE = 0.5
    _BOUND = round(_RATE * (_TRACE_ID_LIMIT + 1))

    @pytest.mark.parametrize(
        "trace_id, expected_decision",
        [
            (0, Decision.RECORD_AND_SAMPLE),
            (_BOUND - 1, Decision.RECORD_AND_SAMPLE),
            (_BOUND, Decision.DROP),
            (_TRACE_ID_LIMIT, Decision.DROP),
            # High-order bits beyond the low 64 must never affect the
            # decision -- only trace_id & TRACE_ID_LIMIT matters.
            ((1 << 70) | 0, Decision.RECORD_AND_SAMPLE),
        ],
    )
    def test_should_sample_matches_documented_formula(
        self, trace_id, expected_decision
    ):
        sampler = TraceIdRatioBased(self._RATE)
        result = sampler.should_sample(None, trace_id, "op")
        assert result.decision == expected_decision


class TestParentContextSampledAlwaysHonored:
    """AC4 gherkin: 'an already-sampled parent context is always honored'
    -- even when the configured root rate would otherwise never sample."""

    def test_sampled_parent_honored_despite_zero_root_rate(self):
        sampler = ParentBased(root=TraceIdRatioBased(0.0))
        ctx = _parent_context(sampled=True)

        result = sampler.should_sample(ctx, trace_id=0x1, name="child")

        assert result.decision == Decision.RECORD_AND_SAMPLE


class TestParentContextNotSampledInheritedByCreateSpan:
    """A custom span created via create_span() (spans.py) nested under a
    sampled-out parent correctly inherits the non-sampled decision --
    proving ParentBased propagation works end-to-end through create_span()
    without any special-casing inside it. Reuses the shared
    active_span_exporter() helper (Story #1586 AC5 pattern) rather than
    mutating spans.py module state directly."""

    def test_create_span_nested_under_dropped_parent_is_not_sampled(self):
        from opentelemetry import context as otel_context
        from opentelemetry.sdk.trace import TracerProvider

        from code_indexer.server.telemetry.spans import create_span
        from tests.unit.server.telemetry.otel_test_support import (
            active_span_exporter,
        )

        sampler = ParentBased(root=TraceIdRatioBased(0.0))
        with active_span_exporter(sampler=sampler):
            root_provider = TracerProvider(sampler=sampler)
            root_tracer = root_provider.get_tracer("test.root")
            root_span = root_tracer.start_span("root")
            assert root_span.get_span_context().trace_flags.sampled is False

            token = otel_context.attach(set_span_in_context(root_span))
            try:
                with create_span("nested") as nested_span:
                    assert nested_span.get_span_context().trace_flags.sampled is False
                    assert nested_span.is_recording() is False
            finally:
                otel_context.detach(token)
                root_span.end()
                root_provider.shutdown()
