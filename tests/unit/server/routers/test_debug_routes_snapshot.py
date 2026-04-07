"""Unit tests for debug memory snapshot - AC1, AC3, AC6.

Story #405: Debug Memory Endpoint

AC1: Memory snapshot returns valid response with required fields.
AC3: Type names include module prefix for non-builtins, bare name for builtins.
AC6: Snapshot overhead reported, no sustained memory growth.
"""

import pytest
from datetime import datetime


# ---------------------------------------------------------------------------
# AC3: _qualify_type_name helper
# ---------------------------------------------------------------------------


class TestQualifyTypeName:
    """AC3: Type names include module prefix for non-builtins."""

    def test_builtin_dict_has_no_prefix(self):
        from code_indexer.server.routers.debug_routes import _qualify_type_name

        assert _qualify_type_name({}) == "dict"

    def test_builtin_list_has_no_prefix(self):
        from code_indexer.server.routers.debug_routes import _qualify_type_name

        assert _qualify_type_name([]) == "list"

    def test_builtin_str_has_no_prefix(self):
        from code_indexer.server.routers.debug_routes import _qualify_type_name

        assert _qualify_type_name("hello") == "str"

    def test_builtin_int_has_no_prefix(self):
        from code_indexer.server.routers.debug_routes import _qualify_type_name

        assert _qualify_type_name(42) == "int"

    def test_builtin_tuple_has_no_prefix(self):
        from code_indexer.server.routers.debug_routes import _qualify_type_name

        assert _qualify_type_name((1, 2)) == "tuple"

    def test_non_builtin_includes_module(self):
        from code_indexer.server.routers.debug_routes import _qualify_type_name
        from datetime import datetime as dt

        result = _qualify_type_name(dt.now())
        assert "datetime" in result

    def test_pathlib_includes_module(self):
        from code_indexer.server.routers.debug_routes import _qualify_type_name
        from pathlib import Path

        result = _qualify_type_name(Path("."))
        assert "pathlib" in result or "Path" in result

    def test_none_module_returns_qualname_only(self):
        from code_indexer.server.routers.debug_routes import _qualify_type_name

        MyType = type("MyType", (), {"__module__": None})
        assert _qualify_type_name(MyType()) == "MyType"

    def test_empty_module_returns_qualname_only(self):
        from code_indexer.server.routers.debug_routes import _qualify_type_name

        MyType = type("MyType", (), {"__module__": ""})
        assert _qualify_type_name(MyType()) == "MyType"


# ---------------------------------------------------------------------------
# AC1 + AC3 + AC6: get_snapshot function
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestGetSnapshot:
    """AC1: Snapshot has all required fields with correct types and values."""

    def setup_method(self):
        import code_indexer.server.routers.debug_routes as mod

        mod._last_snapshot = None

    def test_snapshot_has_timestamp(self):
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        assert "timestamp" in snap
        datetime.fromisoformat(snap["timestamp"].replace("Z", "+00:00"))

    def test_snapshot_timestamp_is_utc(self):
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        ts = snap["timestamp"]
        assert ts.endswith("Z") or "+00:00" in ts

    def test_snapshot_total_objects_positive_int(self):
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        assert "total_objects" in snap
        assert isinstance(snap["total_objects"], int)
        assert snap["total_objects"] > 0

    def test_snapshot_total_size_bytes_positive_int(self):
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        assert "total_size_bytes" in snap
        assert isinstance(snap["total_size_bytes"], int)
        assert snap["total_size_bytes"] > 0

    def test_snapshot_by_count_is_dict(self):
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        assert "by_count" in snap
        assert isinstance(snap["by_count"], dict)
        assert len(snap["by_count"]) > 0

    def test_snapshot_by_count_max_100_entries(self):
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        assert len(snap["by_count"]) <= 100

    def test_snapshot_by_size_bytes_is_dict(self):
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        assert "by_size_bytes" in snap
        assert isinstance(snap["by_size_bytes"], dict)
        assert len(snap["by_size_bytes"]) > 0

    def test_snapshot_by_size_bytes_max_100_entries(self):
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        assert len(snap["by_size_bytes"]) <= 100

    def test_snapshot_overhead_bytes_positive(self):
        """AC6: Overhead must be reported and positive."""
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        assert "snapshot_overhead_bytes" in snap
        assert isinstance(snap["snapshot_overhead_bytes"], int)
        assert snap["snapshot_overhead_bytes"] > 0

    def test_snapshot_by_count_values_positive_ints(self):
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        for key, val in snap["by_count"].items():
            assert isinstance(val, int), f"by_count[{key}] not int"
            assert val > 0, f"by_count[{key}] not positive"

    def test_snapshot_by_size_bytes_values_positive_ints(self):
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        for key, val in snap["by_size_bytes"].items():
            assert isinstance(val, int), f"by_size_bytes[{key}] not int"
            assert val > 0, f"by_size_bytes[{key}] not positive"

    def test_snapshot_by_count_sorted_descending(self):
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        values = list(snap["by_count"].values())
        assert values == sorted(values, reverse=True)

    def test_snapshot_by_size_sorted_descending(self):
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        values = list(snap["by_size_bytes"].values())
        assert values == sorted(values, reverse=True)

    def test_snapshot_stored_in_last_snapshot(self):
        import code_indexer.server.routers.debug_routes as mod
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        assert mod._last_snapshot is not None
        assert mod._last_snapshot["timestamp"] == snap["timestamp"]

    def test_snapshot_builtins_appear_without_prefix(self):
        """AC3: Builtin type names in snapshot have no module prefix."""
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap = get_snapshot()
        all_keys = set(snap["by_count"].keys()) | set(snap["by_size_bytes"].keys())
        builtin_types = {"dict", "list", "str", "int", "tuple", "set", "bytes"}
        found = builtin_types.intersection(all_keys)
        assert len(found) > 0, (
            f"No builtin type names found. Sample keys: {list(all_keys)[:20]}"
        )

    def test_no_sustained_memory_growth(self):
        """AC6: Repeated calls stay within 10% object count tolerance."""
        from code_indexer.server.routers.debug_routes import get_snapshot

        snap1 = get_snapshot()
        snap2 = get_snapshot()
        snap3 = get_snapshot()

        base = snap1["total_objects"]
        for snap in (snap2, snap3):
            ratio = abs(snap["total_objects"] - base) / max(base, 1)
            assert ratio < 0.10, (
                f"Growth too large: base={base}, current={snap['total_objects']}"
            )


@pytest.mark.slow
class TestGetSnapshotSizeofException:
    """Covers the except (TypeError, ValueError, ReferenceError): pass branch."""

    def setup_method(self):
        import code_indexer.server.routers.debug_routes as mod

        mod._last_snapshot = None

    def test_sizeof_typeerror_is_silently_skipped(self, monkeypatch):
        """If sys.getsizeof raises TypeError, snapshot still completes.

        Raises on the 3rd call so that some objects are already sized before
        the exception, ensuring the snapshot returns non-empty by_size_bytes.
        """
        import sys
        import code_indexer.server.routers.debug_routes as mod
        from code_indexer.server.routers.debug_routes import get_snapshot

        real_getsizeof = sys.getsizeof
        call_count = [0]
        # Raise on 3rd call to ensure some objects are counted before hitting
        # the exception branch, so by_size_bytes is still non-empty.
        CALL_TO_FAIL = 3

        def patched_getsizeof(obj, default=None):
            call_count[0] += 1
            if call_count[0] == CALL_TO_FAIL:
                raise TypeError("getsizeof not supported")
            if default is not None:
                return real_getsizeof(obj, default)
            return real_getsizeof(obj)

        monkeypatch.setattr(mod.sys, "getsizeof", patched_getsizeof)

        snap = get_snapshot()
        # Snapshot must complete and return valid structure despite the exception
        assert "total_objects" in snap
        assert snap["total_objects"] > 0
        assert isinstance(snap["by_size_bytes"], dict)
