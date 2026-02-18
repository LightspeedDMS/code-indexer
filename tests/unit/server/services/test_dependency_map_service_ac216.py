"""
Unit tests for DependencyMapService Story #216 ACs.

Tests for:
- AC8: Empty repos excluded from analysis (_enrich_repo_sizes filtering)
- C1: _execute_analysis_passes does NOT call run_pass_3_index (Pass 3 replaced)
- C2: _finalize_analysis calls record_run_metrics with expected metric keys
- H1: _discover_and_assign_new_repos uses public invoke_domain_discovery method
- H3: _generate_index_md output is parseable by _parse_repo_to_domain_mapping (roundtrip)
"""

from pathlib import Path
from unittest.mock import Mock, patch, call

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer


def _make_service(tmp_path):
    """Build a minimal DependencyMapService for testing."""
    gm = Mock()
    gm.golden_repos_dir = str(tmp_path)
    tracking = Mock()
    tracking.get_tracking.return_value = {"status": "pending", "commit_hashes": None}
    config_mgr = Mock()
    analyzer = Mock()
    return DependencyMapService(gm, config_mgr, tracking, analyzer)


# ─────────────────────────────────────────────────────────────────────────────
# AC8: Empty repos excluded from analysis
# ─────────────────────────────────────────────────────────────────────────────


class TestEnrichRepoSizesEmptyFilter:
    """AC8: _enrich_repo_sizes() excludes repos with file_count == 0."""

    def test_excludes_empty_repo(self, tmp_path):
        """AC8: Repo with 0 files is excluded from returned list."""
        svc = _make_service(tmp_path)

        real_repo = tmp_path / "real-repo"
        real_repo.mkdir()
        (real_repo / "main.py").write_text("print('hello')")

        empty_repo = tmp_path / "empty-repo"
        empty_repo.mkdir()

        repo_list = [
            {"alias": "real-repo", "clone_path": str(real_repo)},
            {"alias": "empty-repo", "clone_path": str(empty_repo)},
        ]

        result = svc._enrich_repo_sizes(repo_list)
        result_aliases = [r["alias"] for r in result]
        assert "real-repo" in result_aliases
        assert "empty-repo" not in result_aliases

    def test_keeps_nonempty_repo(self, tmp_path):
        """AC8: Repo with at least one file is kept in returned list."""
        svc = _make_service(tmp_path)

        real_repo = tmp_path / "real-repo"
        real_repo.mkdir()
        (real_repo / "main.py").write_text("print('hello')")

        repo_list = [{"alias": "real-repo", "clone_path": str(real_repo)}]
        result = svc._enrich_repo_sizes(repo_list)
        assert len(result) == 1
        assert result[0]["alias"] == "real-repo"
        assert result[0]["file_count"] >= 1

    def test_excludes_nonexistent_path(self, tmp_path):
        """AC8: Repo with nonexistent clone_path is excluded (treated as 0 files)."""
        svc = _make_service(tmp_path)

        repo_list = [
            {"alias": "missing-repo", "clone_path": str(tmp_path / "does-not-exist")},
        ]
        result = svc._enrich_repo_sizes(repo_list)
        result_aliases = [r["alias"] for r in result]
        assert "missing-repo" not in result_aliases


# ─────────────────────────────────────────────────────────────────────────────
# C1: Pass 3 not called in _execute_analysis_passes
# ─────────────────────────────────────────────────────────────────────────────


class TestPass3NotCalled:
    """C1: _execute_analysis_passes must NOT call run_pass_3_index (replaced by programmatic generation)."""

    def test_execute_analysis_passes_does_not_call_run_pass_3_index(self, tmp_path):
        """C1: run_pass_3_index must never be called during pipeline execution."""
        svc = _make_service(tmp_path)

        # Set up staging dir
        staging_dir = tmp_path / "cidx-meta" / "dependency-map.staging"
        staging_dir.mkdir(parents=True)
        final_dir = tmp_path / "cidx-meta" / "dependency-map"
        cidx_meta_path = tmp_path / "cidx-meta"

        # Domain list returned by Pass 1
        domain_list = [{"name": "auth", "description": "Auth domain", "participating_repos": ["repo1"]}]
        svc._analyzer.run_pass_1_synthesis.return_value = domain_list
        svc._analyzer.run_pass_2_per_domain.return_value = None

        # Create a dummy domain file so Pass 2 char count works
        (staging_dir / "auth.md").write_text("auth content")

        config = Mock()
        config.dependency_map_pass1_max_turns = 10
        config.dependency_map_pass2_max_turns = 10
        config.dependency_map_pass3_max_turns = 10

        paths = {
            "staging_dir": staging_dir,
            "final_dir": final_dir,
            "cidx_meta_path": cidx_meta_path,
            "golden_repos_root": tmp_path,
        }
        repo_list = [{"alias": "repo1", "clone_path": str(tmp_path / "repo1"), "file_count": 5, "total_bytes": 100}]

        svc._execute_analysis_passes(config, paths, repo_list)

        # The critical assertion: run_pass_3_index must NOT be called
        svc._analyzer.run_pass_3_index.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# C2: record_run_metrics called in _finalize_analysis
# ─────────────────────────────────────────────────────────────────────────────


class TestRecordRunMetricsCalled:
    """C2: _finalize_analysis must call tracking_backend.record_run_metrics with expected metric keys."""

    def test_finalize_analysis_calls_record_run_metrics(self, tmp_path):
        """C2: record_run_metrics is called during _finalize_analysis with required keys."""
        svc = _make_service(tmp_path)

        staging_dir = tmp_path / "cidx-meta" / "dependency-map.staging"
        staging_dir.mkdir(parents=True)
        final_dir = tmp_path / "cidx-meta" / "dependency-map"
        cidx_meta_path = tmp_path / "cidx-meta"

        # Write domain files
        domain_list = [
            {"name": "auth", "description": "Auth domain", "participating_repos": ["repo1"]},
        ]
        (staging_dir / "auth.md").write_text("auth content here")

        repo_list = [
            {"alias": "repo1", "clone_path": str(tmp_path / "repo1"), "file_count": 5, "total_bytes": 100},
        ]

        config = Mock()
        config.dependency_map_interval_hours = 24

        paths = {
            "staging_dir": staging_dir,
            "final_dir": final_dir,
            "cidx_meta_path": cidx_meta_path,
            "golden_repos_root": tmp_path,
        }

        # Mock the analyzer methods that _finalize_analysis calls
        svc._analyzer._reconcile_domains_json.return_value = domain_list
        svc._analyzer._generate_index_md.return_value = None

        # Mock the stage-then-swap and reindex to avoid filesystem complexity
        svc._stage_then_swap = Mock()
        svc._reindex_cidx_meta = Mock()
        svc._get_commit_hashes = Mock(return_value={"repo1": "abc123"})

        svc._finalize_analysis(config, paths, repo_list, domain_list)

        # The critical assertion: record_run_metrics must be called
        assert svc._tracking_backend.record_run_metrics.called, (
            "record_run_metrics must be called during _finalize_analysis"
        )

        # Verify required metric keys are present
        call_args = svc._tracking_backend.record_run_metrics.call_args
        metrics = call_args[0][0]  # First positional argument
        required_keys = {
            "timestamp",
            "domain_count",
            "total_chars",
            "edge_count",
            "zero_char_domains",
            "repos_analyzed",
            "repos_skipped",
        }
        for key in required_keys:
            assert key in metrics, f"Missing required metric key: {key!r}"

    def test_finalize_analysis_metrics_have_correct_types(self, tmp_path):
        """C2: record_run_metrics metrics have correct numeric types."""
        svc = _make_service(tmp_path)

        staging_dir = tmp_path / "cidx-meta" / "dependency-map.staging"
        staging_dir.mkdir(parents=True)
        final_dir = tmp_path / "cidx-meta" / "dependency-map"
        cidx_meta_path = tmp_path / "cidx-meta"

        domain_list = [
            {"name": "auth", "description": "Auth domain", "participating_repos": ["repo1"]},
        ]
        (staging_dir / "auth.md").write_text("auth content")

        repo_list = [
            {"alias": "repo1", "clone_path": str(tmp_path / "repo1"), "file_count": 5, "total_bytes": 100},
        ]

        config = Mock()
        config.dependency_map_interval_hours = 24

        paths = {
            "staging_dir": staging_dir,
            "final_dir": final_dir,
            "cidx_meta_path": cidx_meta_path,
            "golden_repos_root": tmp_path,
        }

        svc._analyzer._reconcile_domains_json.return_value = domain_list
        svc._analyzer._generate_index_md.return_value = None
        svc._stage_then_swap = Mock()
        svc._reindex_cidx_meta = Mock()
        svc._get_commit_hashes = Mock(return_value={"repo1": "abc123"})

        svc._finalize_analysis(config, paths, repo_list, domain_list)

        call_args = svc._tracking_backend.record_run_metrics.call_args
        metrics = call_args[0][0]

        assert isinstance(metrics["domain_count"], int)
        assert isinstance(metrics["total_chars"], int)
        assert isinstance(metrics["repos_analyzed"], int)
        assert isinstance(metrics["repos_skipped"], int)
        assert metrics["domain_count"] == 1
        assert metrics["repos_analyzed"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# H1: _discover_and_assign_new_repos uses public invoke_domain_discovery
# ─────────────────────────────────────────────────────────────────────────────


class TestInvokeDomainDiscoveryPublicMethod:
    """H1: _discover_and_assign_new_repos must use public invoke_domain_discovery, not _invoke_claude_cli."""

    def test_discover_and_assign_uses_invoke_domain_discovery(self, tmp_path):
        """H1: Public invoke_domain_discovery is called instead of private _invoke_claude_cli."""
        svc = _make_service(tmp_path)

        # Set up domains file
        dep_map_dir = tmp_path / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        domains_data = [
            {"name": "auth", "description": "Auth domain", "participating_repos": []},
        ]
        (dep_map_dir / "_domains.json").write_text(
            '[{"name": "auth", "description": "Auth domain", "participating_repos": []}]'
        )

        # Mock invoke_domain_discovery to return valid JSON
        svc._analyzer.invoke_domain_discovery.return_value = '[{"repo": "new-repo", "domain": "auth"}]'
        svc._analyzer.build_domain_discovery_prompt.return_value = "prompt"
        svc._analyzer._extract_json = Mock(return_value=[{"repo": "new-repo", "domain": "auth"}])

        config = Mock()
        config.dependency_map_pass_timeout_seconds = 300
        config.dependency_map_delta_max_turns = 5

        new_repos = [{"alias": "new-repo", "clone_path": str(tmp_path / "new-repo")}]
        existing_domains = ["auth"]

        svc._discover_and_assign_new_repos(new_repos, existing_domains, dep_map_dir, config)

        # invoke_domain_discovery must be called (public method)
        assert svc._analyzer.invoke_domain_discovery.called, (
            "invoke_domain_discovery (public) must be called, not _invoke_claude_cli (private)"
        )

    def test_discover_and_assign_does_not_call_private_invoke_claude_cli(self, tmp_path):
        """H1: _invoke_claude_cli (private) must NOT be called directly from _discover_and_assign_new_repos."""
        svc = _make_service(tmp_path)

        dep_map_dir = tmp_path / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        (dep_map_dir / "_domains.json").write_text(
            '[{"name": "auth", "description": "Auth domain", "participating_repos": []}]'
        )

        svc._analyzer.invoke_domain_discovery.return_value = '[]'
        svc._analyzer.build_domain_discovery_prompt.return_value = "prompt"
        svc._analyzer._extract_json = Mock(return_value=[])

        config = Mock()
        config.dependency_map_pass_timeout_seconds = 300
        config.dependency_map_delta_max_turns = 5

        new_repos = [{"alias": "new-repo", "clone_path": str(tmp_path / "new-repo")}]
        existing_domains = ["auth"]

        svc._discover_and_assign_new_repos(new_repos, existing_domains, dep_map_dir, config)

        # _invoke_claude_cli must NOT be called directly on the mock analyzer
        assert not svc._analyzer._invoke_claude_cli.called, (
            "_invoke_claude_cli (private) must not be called directly; use invoke_domain_discovery instead"
        )


# ─────────────────────────────────────────────────────────────────────────────
# H3: Integration test - _generate_index_md output parseable by _parse_repo_to_domain_mapping
# ─────────────────────────────────────────────────────────────────────────────


class TestGenerateIndexMdParseRoundtrip:
    """H3: _generate_index_md output must be correctly parseable by _parse_repo_to_domain_mapping."""

    def _make_real_analyzer(self, tmp_path):
        """Create a real DependencyMapAnalyzer (not a mock) for integration testing."""
        cidx_meta_path = tmp_path / "cidx-meta"
        cidx_meta_path.mkdir(parents=True)
        return DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=cidx_meta_path,
            pass_timeout=300,
        )

    def test_generate_then_parse_returns_correct_domain_mappings(self, tmp_path):
        """H3: Domain mappings generated by _generate_index_md are correctly parsed by _parse_repo_to_domain_mapping."""
        gm = Mock()
        gm.golden_repos_dir = str(tmp_path)
        tracking = Mock()
        tracking.get_tracking.return_value = {"status": "pending", "commit_hashes": None}
        config_mgr = Mock()

        # Use real analyzer for integration test
        real_analyzer = self._make_real_analyzer(tmp_path)
        svc = DependencyMapService(gm, config_mgr, tracking, real_analyzer)

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {"name": "authentication", "description": "Auth domain", "participating_repos": ["repo-auth", "repo-login"]},
            {"name": "data-processing", "description": "Data domain", "participating_repos": ["repo-data"]},
        ]
        repo_list = [
            {"alias": "repo-auth", "clone_path": str(tmp_path / "repo-auth"), "description_summary": "Auth repo"},
            {"alias": "repo-login", "clone_path": str(tmp_path / "repo-login"), "description_summary": "Login repo"},
            {"alias": "repo-data", "clone_path": str(tmp_path / "repo-data"), "description_summary": "Data repo"},
        ]

        # Write domain files so _build_cross_domain_graph doesn't fail
        for domain in domain_list:
            (staging_dir / f"{domain['name']}.md").write_text(f"# {domain['name']} domain")

        # Step 1: Generate _index.md programmatically
        real_analyzer._generate_index_md(staging_dir, domain_list, repo_list)

        index_file = staging_dir / "_index.md"
        assert index_file.exists(), "_index.md must be created by _generate_index_md"

        # Step 2: Parse the generated _index.md
        repo_to_domains = svc._parse_repo_to_domain_mapping(index_file)

        # Step 3: Verify correct mappings are returned
        assert "repo-auth" in repo_to_domains, "repo-auth must appear in parsed mappings"
        assert "repo-login" in repo_to_domains, "repo-login must appear in parsed mappings"
        assert "repo-data" in repo_to_domains, "repo-data must appear in parsed mappings"

        assert "authentication" in repo_to_domains["repo-auth"], (
            "repo-auth must be mapped to 'authentication' domain"
        )
        assert "authentication" in repo_to_domains["repo-login"], (
            "repo-login must be mapped to 'authentication' domain"
        )
        assert "data-processing" in repo_to_domains["repo-data"], (
            "repo-data must be mapped to 'data-processing' domain"
        )

    def test_generate_index_md_uses_domain_singular_header(self, tmp_path):
        """H3: _generate_index_md uses 'Domain' column header (singular), consistent with parser expectations."""
        real_analyzer = self._make_real_analyzer(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        domain_list = [
            {"name": "auth", "description": "Auth", "participating_repos": ["repo1"]},
        ]
        repo_list = [
            {"alias": "repo1", "description_summary": "Repo 1"},
        ]
        (staging_dir / "auth.md").write_text("auth content")

        real_analyzer._generate_index_md(staging_dir, domain_list, repo_list)

        content = (staging_dir / "_index.md").read_text()
        # The matrix table header should use "Domain" (singular) to match parser
        assert "| Repository | Domain |" in content, (
            "_index.md Repo-to-Domain Matrix must use 'Domain' (singular) column header"
        )
