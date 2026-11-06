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

These tests prove the engine's real ceiling (10) and assert that the
config validation agrees with it: the cidx-scip feature's config section
(``scip_dependency_depth`` is a feature setting since modular phase 4d,
bounded by ``cidx_scip.limits`` and enforced by the core's feature config
door on every runtime update, the Web UI config screen's save included --
it replaced ``ServerConfigManager.validate_config``'s check and the core's
own save-time validation).
"""

import pytest

from cidx_scip import limits as scip_limits
from cidx_scip.config import SCIP_CONFIG_SECTION
from cidx_scip.database import queries as scip_queries
from code_indexer.server.features.config import (
    FeatureConfigError,
    validate_runtime_update,
)

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
    """The feature's limits (cidx_scip.limits) must not drift from the
    engine's real ceiling."""

    def test_feature_limits_match_engine_ceiling(self):
        assert scip_limits.MAX_SCIP_DEPENDENCY_DEPTH == ENGINE_MAX_DEPENDENCY_DEPTH
        assert scip_limits.MIN_SCIP_DEPENDENCY_DEPTH == ENGINE_MIN_DEPENDENCY_DEPTH


class TestFeatureSettingValidationMatchesEngineCeiling:
    """A runtime update of the feature setting must reject what the engine
    rejects (the core's feature-config validation, bounds from the section)."""

    @pytest.mark.parametrize("depth", [ENGINE_MAX_DEPENDENCY_DEPTH + 1, 20, 0])
    def test_validation_rejects_out_of_range_depth(self, depth):
        with pytest.raises(FeatureConfigError, match="scip_dependency_depth"):
            validate_runtime_update(
                "scip", SCIP_CONFIG_SECTION, {"scip_dependency_depth": depth}
            )

    def test_validation_accepts_depth_at_engine_ceiling_boundary(self):
        checked = validate_runtime_update(
            "scip",
            SCIP_CONFIG_SECTION,
            {"scip_dependency_depth": ENGINE_MAX_DEPENDENCY_DEPTH},
        )
        assert checked == {"scip_dependency_depth": ENGINE_MAX_DEPENDENCY_DEPTH}
