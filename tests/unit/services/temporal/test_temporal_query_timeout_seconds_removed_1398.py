"""Issue #1398: TEMPORAL_QUERY_TIMEOUT_SECONDS is confirmed dead code (Story
#1291 removed its only consumer -- the multi-provider parallel fan-out
branch). This test proves it no longer exists in source, per the issue's
explicit requirement ("Unit test proving TEMPORAL_QUERY_TIMEOUT_SECONDS no
longer exists in source") -- exposing a dead knob would add a misleading
setting to the exact problem this issue reports.
"""

import code_indexer.services.temporal.temporal_fusion_dispatch as tfd_module


def test_temporal_query_timeout_seconds_constant_removed() -> None:
    assert not hasattr(tfd_module, "TEMPORAL_QUERY_TIMEOUT_SECONDS"), (
        "TEMPORAL_QUERY_TIMEOUT_SECONDS must be deleted entirely (Issue #1398) "
        "-- it is confirmed dead code with zero consumers since Story #1291 "
        "removed the only code path (multi-provider parallel fan-out) that "
        "used it."
    )
