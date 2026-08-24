"""Tests for GitHub issue #1625: scip_dependency_depth ceiling drift.

Root cause: the Web UI Config screen allowed ``scip_dependency_depth`` to be
saved with a value up to 20 (``MAX_SCIP_DEPENDENCY_DEPTH`` in
``server/services/constants.py``, and an independently-maintained ``1-20``
literal in ``server/utils/config_manager.py``'s ``validate_config``), while
the underlying SCIP query engine (``get_dependencies`` / ``get_dependents``
in ``scip/database/queries.py``) hard-rejects any depth above 10.

Call path: ``routes/scip_multi_routes.py`` reads the config value ->
``SCIPMultiService(dependency_depth=...)`` -> ``_get_dependencies_in_repo`` /
``_get_dependents_in_repo`` -> ``engine.get_dependencies``/``get_dependents``
-> ``queries.py`` raises ``ValueError``.

An operator saving 11-20 in the Config screen therefore broke the multi-repo
dependency/dependents endpoints for every repo, failing only at QUERY time
with an opaque ``ValueError`` instead of at CONFIG-SAVE time.

These tests prove the engine's real ceiling (10) and assert that BOTH
config-validation layers (``ServerConfigManager.validate_config`` and the
Web UI's ``_validate_config_section``) now agree with it.
"""

import pytest

from code_indexer.scip.database import queries as scip_queries
from code_indexer.server.services.constants import (
    MIN_SCIP_DEPENDENCY_DEPTH,
    MAX_SCIP_DEPENDENCY_DEPTH,
)
from code_indexer.server.utils.config_manager import ServerConfigManager
from code_indexer.server.web.routes import _validate_config_section

# The SCIP engine's real ceiling for get_dependencies()/get_dependents(),
# hardcoded in scip/database/queries.py. All config-side validation must
# match these values -- this is the Bug #1625 drift under test.
ENGINE_MIN_DEPENDENCY_DEPTH = 1
ENGINE_MAX_DEPENDENCY_DEPTH = 10


class TestEngineDependencyDepthCeiling:
    """Prove the SCIP engine's REAL ceiling for get_dependencies/get_dependents.

    Both functions validate ``depth`` before touching the database
    connection (in the legacy, non-hybrid mode), so passing ``None`` as the
    connection exercises the real production validation code path (no
    mocking) without needing a real SQLite schema: an out-of-range depth
    must raise ``ValueError`` before ``conn`` is ever used, while an
    in-range depth must proceed past the check and only then fail with an
    unrelated error from touching ``None``.
    """

    @pytest.mark.parametrize(
        "query_fn", [scip_queries.get_dependencies, scip_queries.get_dependents]
    )
    def test_rejects_depth_above_engine_ceiling(self, query_fn):
        with pytest.raises(
            ValueError,
            match=f"Depth must be between {ENGINE_MIN_DEPENDENCY_DEPTH} and {ENGINE_MAX_DEPENDENCY_DEPTH}",
        ):
            query_fn(None, symbol_id=0, depth=ENGINE_MAX_DEPENDENCY_DEPTH + 1)

    @pytest.mark.parametrize(
        "query_fn", [scip_queries.get_dependencies, scip_queries.get_dependents]
    )
    def test_accepts_depth_at_engine_ceiling_boundary(self, query_fn):
        # depth == ceiling must pass the range check and proceed to touch
        # `conn` (None here), proving the ValueError-for-range path was
        # NOT taken.
        with pytest.raises(AttributeError):
            query_fn(None, symbol_id=0, depth=ENGINE_MAX_DEPENDENCY_DEPTH)


class TestConstantsMatchEngineCeiling:
    """MAX_SCIP_DEPENDENCY_DEPTH must never exceed the engine's real ceiling."""

    def test_max_scip_dependency_depth_matches_engine_ceiling(self):
        assert MAX_SCIP_DEPENDENCY_DEPTH == ENGINE_MAX_DEPENDENCY_DEPTH, (
            "MAX_SCIP_DEPENDENCY_DEPTH must equal the SCIP engine's real "
            "get_dependencies()/get_dependents() ceiling, enforced in "
            "scip/database/queries.py -- Bug #1625 drift."
        )

    def test_min_scip_dependency_depth_matches_engine_floor(self):
        assert MIN_SCIP_DEPENDENCY_DEPTH == ENGINE_MIN_DEPENDENCY_DEPTH


class TestConfigManagerValidationMatchesEngineCeiling:
    """ServerConfigManager.validate_config must reject what the engine rejects."""

    @pytest.mark.parametrize("depth", [ENGINE_MAX_DEPENDENCY_DEPTH + 1, 20, 0])
    def test_validation_rejects_out_of_range_depth(self, tmp_path, depth):
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.scip_config.scip_dependency_depth = depth

        with pytest.raises(ValueError, match="scip_dependency_depth"):
            config_manager.validate_config(config)

    def test_validation_accepts_depth_at_engine_ceiling_boundary(self, tmp_path):
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.scip_config.scip_dependency_depth = ENGINE_MAX_DEPENDENCY_DEPTH

        config_manager.validate_config(config)  # Should not raise


class TestWebConfigScreenSaveTimeValidationMatchesEngineCeiling:
    """The Config screen's own save-time validation must reject 11-20.

    This is the correct place to catch the drift -- at config-save time,
    not as an opaque ValueError from the query engine later.
    """

    @pytest.mark.parametrize("depth", [ENGINE_MAX_DEPENDENCY_DEPTH + 1, 20])
    def test_rejects_out_of_range_depth_at_save_time(self, depth):
        error = _validate_config_section("scip", {"scip_dependency_depth": depth})
        assert error is not None
        assert "SCIP Dependency Depth" in error

    @pytest.mark.parametrize(
        "depth", [ENGINE_MIN_DEPENDENCY_DEPTH, ENGINE_MAX_DEPENDENCY_DEPTH]
    )
    def test_accepts_boundary_depth_at_save_time(self, depth):
        error = _validate_config_section("scip", {"scip_dependency_depth": depth})
        assert error is None
