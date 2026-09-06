"""Story #1787 AC12/AC13/AC15: distinct terminal statuses for the X-Ray
graph-build memory-governor integration.

Rule 13 (anti-silent-failure) discriminating requirement: every one of
these statuses must be a genuinely DISTINCT value -- a wrong
implementation that collapsed two abort reasons onto the same value
would still "have four members" by naive inspection but would make it
impossible to tell them apart from a caller's perspective. This test
suite proves distinctness by VALUE, not merely by enum member count.
"""

from __future__ import annotations

from code_indexer.server.services.xray_graph_governor.status import (
    GraphBuildAbortStatus,
    GraphBuildPhase,
)


class TestGraphBuildAbortStatus:
    def test_has_exactly_five_members(self):
        # Dual-review defect H5 fix added ADMISSION_DENIED_ESTIMATE_EXCEEDS_LIMIT.
        assert len(list(GraphBuildAbortStatus)) == 5

    def test_all_five_values_are_pairwise_distinct(self):
        values = [status.value for status in GraphBuildAbortStatus]
        assert len(set(values)) == 5, (
            "every GraphBuildAbortStatus member must serialize to a DISTINCT value"
        )

    def test_names_match_the_amendment_text_exactly(self):
        assert (
            GraphBuildAbortStatus.ADMISSION_DENIED_GATE1.value
            == "admission_denied_gate1"
        )
        assert (
            GraphBuildAbortStatus.ADMISSION_DENIED_GATE2.value
            == "admission_denied_gate2"
        )
        assert (
            GraphBuildAbortStatus.ADMISSION_DENIED_ESTIMATE_EXCEEDS_LIMIT.value
            == "admission_denied_estimate_exceeds_limit"
        )
        assert (
            GraphBuildAbortStatus.ABORTED_MEMORY_PRESSURE.value
            == "aborted_memory_pressure"
        )
        assert (
            GraphBuildAbortStatus.ABORTED_MEMORY_LIMIT.value == "aborted_memory_limit"
        )


class TestGraphBuildPhase:
    def test_has_the_four_ac13_boundaries_in_the_documented_order(self):
        assert [phase.value for phase in GraphBuildPhase] == [
            "extract",
            "bind",
            "analyze",
            "refine",
        ]

    def test_all_four_phase_values_are_pairwise_distinct(self):
        values = [phase.value for phase in GraphBuildPhase]
        assert len(set(values)) == 4
