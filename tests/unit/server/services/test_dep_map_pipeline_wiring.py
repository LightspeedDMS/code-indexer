"""
Unit tests for pipeline wiring: _finalize_analysis calls programmatic index generation.

Story #216 - AC2 pipeline wiring:
_finalize_analysis must call _reconcile_domains_json (AC4) then _generate_index_md (AC2)
instead of run_pass_3_index (Claude-based Pass 3).
"""

from pathlib import Path
from unittest.mock import Mock, patch

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_service_for_finalize(tmp_path):
    """Build a DependencyMapService with a mocked analyzer for finalize testing."""
    gm = Mock()
    gm.golden_repos_dir = str(tmp_path)
    tracking = Mock()
    tracking.get_tracking.return_value = {"status": "running", "commit_hashes": None}
    config_mgr = Mock()
    config = Mock()
    config.dependency_map_interval_hours = 24
    config_mgr.get_claude_integration_config.return_value = config
    analyzer = Mock()
    analyzer._reconcile_domains_json.return_value = [
        {"name": "auth", "participating_repos": ["auth-svc"]},
    ]
    return DependencyMapService(gm, config_mgr, tracking, analyzer)


def _make_paths(tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    return {
        "staging_dir": staging_dir,
        "final_dir": tmp_path / "final",
        "cidx_meta_path": tmp_path / "cidx-meta",
        "golden_repos_root": tmp_path,
    }


class TestFinalizeAnalysisPipelineWiring:
    """Pipeline wiring: _finalize_analysis calls programmatic index generation."""

    def test_finalize_analysis_calls_reconcile_domains_json(self, tmp_path):
        """_finalize_analysis calls _reconcile_domains_json before _generate_index_md."""
        svc = _make_service_for_finalize(tmp_path)
        domain_list = [{"name": "auth", "participating_repos": ["auth-svc"]}]
        repo_list = [{"alias": "auth-svc", "clone_path": str(tmp_path)}]
        config = Mock()
        config.dependency_map_interval_hours = 24

        with patch.object(svc, "_stage_then_swap"), \
             patch.object(svc, "_reindex_cidx_meta"), \
             patch.object(svc, "_get_commit_hashes", return_value={}):
            svc._finalize_analysis(config, _make_paths(tmp_path), repo_list, domain_list)

        svc._analyzer._reconcile_domains_json.assert_called_once_with(
            _make_paths(tmp_path)["staging_dir"], domain_list
        )

    def test_finalize_analysis_calls_generate_index_md(self, tmp_path):
        """_finalize_analysis calls _generate_index_md with reconciled domains."""
        svc = _make_service_for_finalize(tmp_path)
        domain_list = [{"name": "auth", "participating_repos": ["auth-svc"]}]
        reconciled = [{"name": "auth", "participating_repos": ["auth-svc"]}]
        svc._analyzer._reconcile_domains_json.return_value = reconciled
        repo_list = [{"alias": "auth-svc", "clone_path": str(tmp_path)}]
        config = Mock()
        config.dependency_map_interval_hours = 24
        paths = _make_paths(tmp_path)

        with patch.object(svc, "_stage_then_swap"), \
             patch.object(svc, "_reindex_cidx_meta"), \
             patch.object(svc, "_get_commit_hashes", return_value={}):
            svc._finalize_analysis(config, paths, repo_list, domain_list)

        svc._analyzer._generate_index_md.assert_called_once_with(
            paths["staging_dir"], reconciled, repo_list
        )

    def test_finalize_analysis_does_not_call_run_pass3_index(self, tmp_path):
        """_finalize_analysis does NOT call run_pass_3_index (replaced by programmatic gen)."""
        svc = _make_service_for_finalize(tmp_path)
        domain_list = [{"name": "auth", "participating_repos": ["auth-svc"]}]
        repo_list = [{"alias": "auth-svc", "clone_path": str(tmp_path)}]
        config = Mock()
        config.dependency_map_interval_hours = 24

        with patch.object(svc, "_stage_then_swap"), \
             patch.object(svc, "_reindex_cidx_meta"), \
             patch.object(svc, "_get_commit_hashes", return_value={}):
            svc._finalize_analysis(config, _make_paths(tmp_path), repo_list, domain_list)

        svc._analyzer.run_pass_3_index.assert_not_called()

    def test_reconcile_called_before_generate_index(self, tmp_path):
        """_reconcile_domains_json is called before _generate_index_md."""
        svc = _make_service_for_finalize(tmp_path)
        call_order = []

        def track_reconcile(*args, **kwargs):
            call_order.append("reconcile")
            return args[1]

        def track_generate(*args, **kwargs):
            call_order.append("generate")

        svc._analyzer._reconcile_domains_json.side_effect = track_reconcile
        svc._analyzer._generate_index_md.side_effect = track_generate

        domain_list = [{"name": "auth", "participating_repos": ["auth-svc"]}]
        repo_list = [{"alias": "auth-svc", "clone_path": str(tmp_path)}]
        config = Mock()
        config.dependency_map_interval_hours = 24

        with patch.object(svc, "_stage_then_swap"), \
             patch.object(svc, "_reindex_cidx_meta"), \
             patch.object(svc, "_get_commit_hashes", return_value={}):
            svc._finalize_analysis(config, _make_paths(tmp_path), repo_list, domain_list)

        assert call_order == ["reconcile", "generate"], (
            f"Expected reconcile before generate, got: {call_order}"
        )
