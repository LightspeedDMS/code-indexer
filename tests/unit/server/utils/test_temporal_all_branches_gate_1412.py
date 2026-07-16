"""Unit tests for Story #1412 - Gate golden/server temporal all-branches
indexing behind a server config flag (ship disabled).

Section 1: IndexingConfig.temporal_all_branches_enabled default + override.
"""

from code_indexer.server.utils.config_manager import IndexingConfig


class TestIndexingConfigTemporalAllBranchesEnabledDefault:
    """AC1 (Scenario 1): gate must default to False."""

    def test_temporal_all_branches_enabled_default_is_false(self) -> None:
        cfg = IndexingConfig()
        assert cfg.temporal_all_branches_enabled is False

    def test_temporal_all_branches_enabled_accepts_true_override(self) -> None:
        cfg = IndexingConfig(temporal_all_branches_enabled=True)
        assert cfg.temporal_all_branches_enabled is True

    def test_temporal_all_branches_enabled_accepts_false_override(self) -> None:
        cfg = IndexingConfig(temporal_all_branches_enabled=False)
        assert cfg.temporal_all_branches_enabled is False
