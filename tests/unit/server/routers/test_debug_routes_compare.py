"""Unit tests for debug memory compare endpoint - AC4, AC5.

Story #405: Debug Memory Endpoint

AC4: GET /debug/memory-compare?baseline={timestamp} returns delta between
     the stored baseline snapshot and a freshly taken current snapshot.
AC5: GET /debug/memory-compare?baseline=<unknown> returns 404 (no baseline found).
"""

from datetime import datetime


# ---------------------------------------------------------------------------
# AC4 + AC5: compare_snapshot function
# ---------------------------------------------------------------------------


class TestCompareSnapshot:
    """AC4/AC5: compare_snapshot logic."""

    def setup_method(self):
        import code_indexer.server.routers.debug_routes as mod

        mod._last_snapshot = None

    def test_no_stored_snapshot_returns_none(self):
        """AC5: Returns None when _last_snapshot is None."""
        from code_indexer.server.routers.debug_routes import compare_snapshot

        assert compare_snapshot("2099-01-01T00:00:00Z") is None

    def test_wrong_timestamp_returns_none(self):
        """AC5: Returns None when stored timestamp does not match."""
        from code_indexer.server.routers.debug_routes import (
            get_snapshot,
            compare_snapshot,
        )

        get_snapshot()
        assert compare_snapshot("2099-01-01T00:00:00Z") is None

    def test_matching_timestamp_returns_result(self):
        """AC4: Matching timestamp returns a compare result dict."""
        from code_indexer.server.routers.debug_routes import (
            get_snapshot,
            compare_snapshot,
        )

        snap = get_snapshot()
        result = compare_snapshot(snap["timestamp"])
        assert result is not None

    def test_result_contains_baseline_timestamp(self):
        """AC4: baseline_timestamp echoes the requested timestamp."""
        from code_indexer.server.routers.debug_routes import (
            get_snapshot,
            compare_snapshot,
        )

        snap = get_snapshot()
        result = compare_snapshot(snap["timestamp"])
        assert result["baseline_timestamp"] == snap["timestamp"]

    def test_result_contains_current_timestamp(self):
        """AC4: current_timestamp is present and parseable ISO 8601."""
        from code_indexer.server.routers.debug_routes import (
            get_snapshot,
            compare_snapshot,
        )

        snap = get_snapshot()
        result = compare_snapshot(snap["timestamp"])
        assert "current_timestamp" in result
        datetime.fromisoformat(result["current_timestamp"].replace("Z", "+00:00"))

    def test_result_contains_delta_objects_as_int(self):
        """AC4: delta_objects is an integer (may be 0, positive, or negative)."""
        from code_indexer.server.routers.debug_routes import (
            get_snapshot,
            compare_snapshot,
        )

        snap = get_snapshot()
        result = compare_snapshot(snap["timestamp"])
        assert "delta_objects" in result
        assert isinstance(result["delta_objects"], int)

    def test_result_contains_delta_size_bytes_as_int(self):
        """AC4: delta_size_bytes is an integer."""
        from code_indexer.server.routers.debug_routes import (
            get_snapshot,
            compare_snapshot,
        )

        snap = get_snapshot()
        result = compare_snapshot(snap["timestamp"])
        assert "delta_size_bytes" in result
        assert isinstance(result["delta_size_bytes"], int)

    def test_result_contains_by_count_diff_as_dict(self):
        """AC4: by_count_diff is a dict of type count changes."""
        from code_indexer.server.routers.debug_routes import (
            get_snapshot,
            compare_snapshot,
        )

        snap = get_snapshot()
        result = compare_snapshot(snap["timestamp"])
        assert "by_count_diff" in result
        assert isinstance(result["by_count_diff"], dict)

    def test_result_contains_by_size_diff_as_dict(self):
        """AC4: by_size_diff is a dict of type size changes."""
        from code_indexer.server.routers.debug_routes import (
            get_snapshot,
            compare_snapshot,
        )

        snap = get_snapshot()
        result = compare_snapshot(snap["timestamp"])
        assert "by_size_diff" in result
        assert isinstance(result["by_size_diff"], dict)

    def test_compare_updates_last_snapshot(self):
        """After compare, _last_snapshot is the current (not baseline) snapshot."""
        import code_indexer.server.routers.debug_routes as mod
        from code_indexer.server.routers.debug_routes import (
            get_snapshot,
            compare_snapshot,
        )

        snap = get_snapshot()
        result = compare_snapshot(snap["timestamp"])
        assert result is not None
        assert mod._last_snapshot["timestamp"] == result["current_timestamp"]


# ---------------------------------------------------------------------------
# AC5: HTTP endpoint 404 scenario
# ---------------------------------------------------------------------------


class TestMemoryCompareEndpoint:
    """AC4/AC5: compare endpoint HTTP behavior."""

    def setup_method(self):
        import code_indexer.server.routers.debug_routes as mod

        mod._last_snapshot = None

    def test_compare_missing_baseline_returns_none(self):
        """AC5: compare_snapshot(unknown_ts) returns None - maps to 404 in handler."""
        from code_indexer.server.routers.debug_routes import compare_snapshot

        result = compare_snapshot("2099-01-01T00:00:00Z")
        assert result is None

    def test_compare_full_workflow_all_fields_present(self):
        """AC4: Full workflow produces a complete diff with all required fields."""
        from code_indexer.server.routers.debug_routes import (
            get_snapshot,
            compare_snapshot,
        )

        baseline = get_snapshot()
        diff = compare_snapshot(baseline["timestamp"])
        assert diff is not None

        required = [
            "baseline_timestamp",
            "current_timestamp",
            "delta_objects",
            "delta_size_bytes",
            "by_count_diff",
            "by_size_diff",
        ]
        for field in required:
            assert field in diff, f"Missing field: {field}"
