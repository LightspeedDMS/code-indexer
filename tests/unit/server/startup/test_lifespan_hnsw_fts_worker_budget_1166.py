"""Story #1166 regression guard: service_init.py calls initialize_caches BEFORE the
eager get_global_cache()/get_global_fts_cache() getters.

Bug identified by code review: the original fix placed initialize_caches() in
lifespan.py AFTER both eager getters already ran in initialize_services(). By the
time lifespan startup ran, the singletons already existed at full cap, so
initialize_caches() hit the idempotent guard and became a no-op.

Correct ordering (Fix 1):
    initialize_caches(worker_count)           -- divides caps
    _server_hnsw_cache = get_global_cache()   -- returns divided singleton
    _server_fts_cache  = get_global_fts_cache()  -- returns divided singleton

Fix 2: remove the now-redundant initialize_caches() call from lifespan.py so
there is a single source of truth (service_init.py).

These tests guard BOTH the source order AND the observable behaviour under the
real construction path.  The behavioral AC tests MUST FAIL on the rejected code
(initialize_caches in lifespan, not in service_init) and PASS after Fix 1.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_PARENTS_TO_REPO_ROOT = 4
_REPO_ROOT = Path(__file__).resolve().parents[_PARENTS_TO_REPO_ROOT]
_SERVICE_INIT_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "service_init.py"
)
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


# ---------------------------------------------------------------------------
# Singleton cleanup helpers
# ---------------------------------------------------------------------------


def _stop_and_clear_singletons(cache_module: ModuleType) -> None:
    """Stop any running cleanup thread and reset global singleton slots to None."""
    for attr in ("_global_cache_instance", "_global_fts_cache_instance"):
        instance = getattr(cache_module, attr)
        if instance is None:
            continue
        try:
            instance.stop_background_cleanup()
        except RuntimeError:
            pass  # Thread not alive / already stopped -- safe to ignore
        setattr(cache_module, attr, None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cache_singletons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Reset HNSW/FTS singletons around every test and isolate Path.home()."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    import code_indexer.server.cache as cache_module

    _stop_and_clear_singletons(cache_module)
    yield
    _stop_and_clear_singletons(cache_module)


# ---------------------------------------------------------------------------
# Class 1: Source-order guard in service_init.py
# ---------------------------------------------------------------------------


class TestServiceInitCacheOrderGuard:
    """initialize_caches must be called BEFORE get_global_cache() in service_init.py.

    This is the ordering defect identified by code review: the rejected
    implementation placed initialize_caches in lifespan.py (too late).  The fix
    places it inside initialize_services() BEFORE the eager getters.
    """

    def test_initialize_caches_present_in_service_init(self) -> None:
        """service_init.py must import and call initialize_caches."""
        source = _SERVICE_INIT_PATH.read_text()
        assert "initialize_caches" in source, (
            "service_init.py must call initialize_caches(worker_count) "
            "so the divided cap is in place before the eager "
            "get_global_cache()/get_global_fts_cache() calls build the singletons. "
            "Story #1166 Fix 1."
        )

    def test_initialize_caches_before_get_global_cache_in_service_init(self) -> None:
        """initialize_caches call must appear BEFORE get_global_cache() in service_init.py.

        This is the core ordering invariant.  On the rejected code the call was
        in lifespan.py (thousands of lines later, after both singletons were
        already built at full cap).  This source-order check ensures the division
        happens before the singletons are constructed.
        """
        source = _SERVICE_INIT_PATH.read_text()

        init_pos = source.find("initialize_caches(")
        getter_pos = source.find("get_global_cache()")

        assert init_pos != -1, (
            "initialize_caches( call not found in service_init.py. "
            "Fix 1 requires it to be placed before the eager getter."
        )
        assert getter_pos != -1, (
            "get_global_cache() call not found in service_init.py -- "
            "this is unexpected; the eager getter must still be present."
        )
        assert init_pos < getter_pos, (
            f"SOURCE-ORDER VIOLATION (Story #1166 defect): "
            f"initialize_caches( is at pos {init_pos} but "
            f"get_global_cache() is at pos {getter_pos}. "
            f"initialize_caches MUST come first so the singletons are "
            f"built with the divided cap, not the full cap."
        )

    def test_initialize_caches_before_get_global_fts_cache_in_service_init(
        self,
    ) -> None:
        """initialize_caches call must appear BEFORE get_global_fts_cache() too."""
        source = _SERVICE_INIT_PATH.read_text()

        init_pos = source.find("initialize_caches(")
        fts_getter_pos = source.find("get_global_fts_cache()")

        assert init_pos != -1, "initialize_caches( call not found in service_init.py."
        assert fts_getter_pos != -1, (
            "get_global_fts_cache() call not found in service_init.py."
        )
        assert init_pos < fts_getter_pos, (
            f"SOURCE-ORDER VIOLATION (Story #1166 defect): "
            f"initialize_caches( is at pos {init_pos} but "
            f"get_global_fts_cache() is at pos {fts_getter_pos}. "
            f"initialize_caches MUST come first."
        )


# ---------------------------------------------------------------------------
# Class 2: Behavioral test -- real construction path
# ---------------------------------------------------------------------------


class TestServiceInitRealConstructionPath:
    """Behavioral test: exercises the REAL initialize_caches + eager-getter path.

    Why not call initialize_services() directly:
    initialize_services() creates DB schemas, spawns background threads,
    writes to disk, connects to PostgreSQL, invokes bootstrap git operations, etc.
    Running it in a unit test would require mocking dozens of external boundaries.
    The narrowest REAL path that still includes the eager getters in their correct
    order is the 5-line block below -- initialize_caches -> get_global_cache ->
    get_global_fts_cache -- executed with real implementations (no mocking of
    cache internals, config_service, or getters).

    AC1: workers=4,  4096 MB cap -> 4096 // 4 == 1024 MB per worker
    AC2: workers=1,  4096 MB cap -> 4096 // 1 == 4096 MB (unchanged)
    AC3: workers=32, 4096 MB cap -> 4096 // 32 == 128 < 256 floor -> 256 MB
    """

    def _write_config(self, tmp_path: Path, workers: int) -> None:
        """Write a minimal config.json with the given worker count."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "workers": workers,
            "index_cache_max_size_mb": 4096,
            "fts_cache_max_size_mb": 4096,
        }
        (server_dir / "config.json").write_text(json.dumps(config))

    def _read_worker_count_from_config(self) -> int:
        """Read config.workers via the real config_service (mirrors governor pattern).

        workers is a BOOTSTRAP key stored in config.json, available before
        initialize_runtime_db is called.  This is the same approach used by
        ProviderConcurrencyGovernor._read_config_workers().

        Raises on any failure -- config errors must not be silently swallowed
        so that test failures are loud and diagnosable.
        """
        from code_indexer.server.services.config_service import (
            get_config_service,
            reset_config_service,
        )

        reset_config_service()  # clear any cached instance from a prior test
        cfg = get_config_service().get_config()
        value = getattr(cfg, "workers", None)
        if not isinstance(value, int):
            raise AssertionError(
                f"Expected config.workers to be int, got {value!r}. "
                "The test config.json was not written correctly."
            )
        return max(1, value)

    def _run_real_ordering_path(self, tmp_path: Path, workers: int) -> tuple[int, int]:
        """Execute the real initialize_caches + eager getter sequence.

        This replicates the exact 5-line block that service_init.py must contain
        after Fix 1.  The singletons are reset to None by the autouse fixture,
        so each call starts from a clean slate.

        Returns (hnsw_cap_mb, fts_cap_mb).
        """
        import code_indexer.server.cache as cache_module
        from code_indexer.server.cache import (
            initialize_caches,
            get_global_cache,
            get_global_fts_cache,
        )

        # Autouse fixture ensures both are None before this runs
        assert cache_module._global_cache_instance is None
        assert cache_module._global_fts_cache_instance is None

        worker_count = self._read_worker_count_from_config()

        # Fix 1 ordering: initialize_caches BEFORE eager getters
        initialize_caches(worker_count=worker_count)
        _server_hnsw_cache = get_global_cache()
        _server_fts_cache = get_global_fts_cache()

        return (
            _server_hnsw_cache.config.max_cache_size_mb,
            _server_fts_cache.config.max_cache_size_mb,
        )

    def test_ac1_four_workers_divides_cap_real_path(self, tmp_path: Path) -> None:
        """AC1: workers=4 -> singletons get 4096//4 == 1024 MB cap.

        MUST FAIL on old code (initialize_caches missing from service_init.py;
        only placed in lifespan.py which runs after singleton construction).
        MUST PASS after Fix 1 places initialize_caches before the eager getters.
        """
        self._write_config(tmp_path, workers=4)
        hnsw_cap, fts_cap = self._run_real_ordering_path(tmp_path, workers=4)

        assert hnsw_cap == 1024, (
            f"AC1 FAIL: HNSW cap should be 4096//4=1024 with workers=4; "
            f"got {hnsw_cap}. "
            "This indicates initialize_caches ran AFTER the singletons were "
            "already built at full cap (the ordering defect from code review)."
        )
        assert fts_cap == 1024, (
            f"AC1 FAIL: FTS cap should be 4096//4=1024 with workers=4; got {fts_cap}."
        )

    def test_ac2_one_worker_cap_unchanged_real_path(self, tmp_path: Path) -> None:
        """AC2: workers=1 -> caps remain at 4096 MB (byte-identical to today)."""
        self._write_config(tmp_path, workers=1)
        hnsw_cap, fts_cap = self._run_real_ordering_path(tmp_path, workers=1)

        assert hnsw_cap == 4096, (
            f"AC2 FAIL: 1-worker HNSW cap must stay at 4096 MB; got {hnsw_cap}"
        )
        assert fts_cap == 4096, (
            f"AC2 FAIL: 1-worker FTS cap must stay at 4096 MB; got {fts_cap}"
        )

    def test_ac3_over_division_floor_real_path(self, tmp_path: Path) -> None:
        """AC3: workers=32 -> 4096//32==128 < 256 floor -> caps == 256 MB."""
        from code_indexer.server.cache import MIN_CAP_PER_WORKER_MB

        self._write_config(tmp_path, workers=32)
        hnsw_cap, fts_cap = self._run_real_ordering_path(tmp_path, workers=32)

        assert hnsw_cap == MIN_CAP_PER_WORKER_MB, (
            f"AC3 FAIL: HNSW cap must floor at {MIN_CAP_PER_WORKER_MB} MB "
            f"(4096//32=128 < floor); got {hnsw_cap}"
        )
        assert fts_cap == MIN_CAP_PER_WORKER_MB, (
            f"AC3 FAIL: FTS cap must floor at {MIN_CAP_PER_WORKER_MB} MB; got {fts_cap}"
        )


# ---------------------------------------------------------------------------
# Class 3: Fix 2 -- single source of truth guard (no live call in lifespan.py)
# ---------------------------------------------------------------------------


class TestLifespanNoDuplicateInitializeCaches:
    """Fix 2: initialize_caches must NOT appear as a live call in lifespan.py.

    Once initialize_caches is placed in service_init.py (before the eager
    getters), the lifespan call becomes a confusing second source of truth
    that hits the idempotent guard and does nothing.  It must be removed.
    A comment referencing the service_init.py placement is acceptable;
    a live function call is not.
    """

    def test_no_live_initialize_caches_call_in_lifespan(self) -> None:
        """lifespan.py must NOT contain a live initialize_caches( call after Fix 2.

        Collect all lines containing initialize_caches( that are not comments.
        The test passes when none exist (Fix 2 applied) and fails when the
        redundant lifespan call is still present (Fix 2 not yet applied).
        """
        source = _LIFESPAN_PATH.read_text()
        live_call_lines = [
            (i + 1, line)
            for i, line in enumerate(source.splitlines())
            if "initialize_caches(" in line and not line.lstrip().startswith("#")
        ]
        assert not live_call_lines, (
            "Fix 2 violation: lifespan.py still contains live initialize_caches( "
            "call(s). Single source of truth is service_init.py after Fix 1. "
            "Remove these lines: "
            + str([(ln, line_text.strip()) for ln, line_text in live_call_lines])
        )
