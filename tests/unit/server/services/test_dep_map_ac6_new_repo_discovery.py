"""
Unit tests for AC6: New repo discovery in delta refresh.

Story #216 AC6:
- When new repos are detected, _discover_and_assign_new_repos is called
- build_domain_discovery_prompt is invoked with new_repos and existing_domains
- _domains.json is updated with new repo assignments
- Affected domains are returned for re-analysis
- run_delta_analysis replaces the stub with real discovery logic
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_service(tmp_path):
    """Build a DependencyMapService for AC6 discovery testing."""
    gm = Mock()
    gm.golden_repos_dir = str(tmp_path)
    tracking = Mock()
    tracking.get_tracking.return_value = {
        "status": "completed",
        "commit_hashes": json.dumps({}),
        "next_run": None,
    }
    config_mgr = Mock()
    config = Mock()
    config.dependency_map_enabled = True
    config.dependency_map_interval_hours = 24
    config.dependency_map_delta_max_turns = 5
    config.dependency_map_pass_timeout_seconds = 120
    config_mgr.get_claude_integration_config.return_value = config
    analyzer = Mock()
    return DependencyMapService(gm, config_mgr, tracking, analyzer)


def _setup_depmap_dir(tmp_path, domains=None):
    """Create dependency-map directory with _domains.json."""
    depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
    depmap_dir.mkdir(parents=True)
    if domains is None:
        domains = [
            {"name": "auth", "description": "Auth domain", "participating_repos": ["existing-svc"]},
        ]
    (depmap_dir / "_domains.json").write_text(json.dumps(domains))
    (depmap_dir / "auth.md").write_text(
        "---\ndomain: auth\n---\n\n# Domain Analysis: auth\n\n## Overview\nTest.\n"
    )
    return depmap_dir


class TestDiscoverAndAssignNewRepos:
    """AC6: _discover_and_assign_new_repos method behavior."""

    def test_method_exists_on_service(self, tmp_path):
        """AC6: DependencyMapService has _discover_and_assign_new_repos method."""
        svc = _make_service(tmp_path)
        assert hasattr(svc, "_discover_and_assign_new_repos"), (
            "DependencyMapService must have _discover_and_assign_new_repos method"
        )

    def test_calls_build_domain_discovery_prompt(self, tmp_path):
        """AC6: _discover_and_assign_new_repos calls build_domain_discovery_prompt."""
        svc = _make_service(tmp_path)
        depmap_dir = _setup_depmap_dir(tmp_path)

        new_repos = [{"alias": "new-svc", "clone_path": str(tmp_path / "new-svc")}]
        existing_domains = ["auth"]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value='[{"repo": "new-svc", "domains": ["auth"]}]'):
            svc._discover_and_assign_new_repos(
                new_repos=new_repos,
                existing_domains=existing_domains,
                dependency_map_dir=depmap_dir,
                config=Mock(dependency_map_delta_max_turns=5, dependency_map_pass_timeout_seconds=120),
            )

        svc._analyzer.build_domain_discovery_prompt.assert_called_once_with(
            new_repos, existing_domains
        )

    def test_updates_domains_json_with_new_repo(self, tmp_path):
        """AC6: _domains.json is updated to include new repo in the assigned domain."""
        svc = _make_service(tmp_path)
        depmap_dir = _setup_depmap_dir(tmp_path)

        new_repos = [{"alias": "new-svc", "clone_path": str(tmp_path / "new-svc")}]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value='[{"repo": "new-svc", "domains": ["auth"]}]'):
            svc._discover_and_assign_new_repos(
                new_repos=new_repos,
                existing_domains=["auth"],
                dependency_map_dir=depmap_dir,
                config=Mock(dependency_map_delta_max_turns=5, dependency_map_pass_timeout_seconds=120),
            )

        updated = json.loads((depmap_dir / "_domains.json").read_text())
        auth_domain = next((d for d in updated if d["name"] == "auth"), None)
        assert auth_domain is not None
        assert "new-svc" in auth_domain["participating_repos"]

    def test_returns_affected_domain_names(self, tmp_path):
        """AC6: Method returns set of affected domain names for re-analysis."""
        svc = _make_service(tmp_path)
        depmap_dir = _setup_depmap_dir(tmp_path)

        new_repos = [{"alias": "new-svc", "clone_path": str(tmp_path / "new-svc")}]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value='[{"repo": "new-svc", "domains": ["auth"]}]'):
            affected = svc._discover_and_assign_new_repos(
                new_repos=new_repos,
                existing_domains=["auth"],
                dependency_map_dir=depmap_dir,
                config=Mock(dependency_map_delta_max_turns=5, dependency_map_pass_timeout_seconds=120),
            )

        assert isinstance(affected, set)
        assert "auth" in affected

    def test_handles_unparseable_discovery_response_gracefully(self, tmp_path):
        """AC6: If Claude returns unparseable JSON, method does not crash."""
        svc = _make_service(tmp_path)
        depmap_dir = _setup_depmap_dir(tmp_path)

        new_repos = [{"alias": "new-svc", "clone_path": str(tmp_path / "new-svc")}]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value="This is not JSON at all"):
            affected = svc._discover_and_assign_new_repos(
                new_repos=new_repos,
                existing_domains=["auth"],
                dependency_map_dir=depmap_dir,
                config=Mock(dependency_map_delta_max_turns=5, dependency_map_pass_timeout_seconds=120),
            )

        # Should return empty set or gracefully handle, not crash
        assert isinstance(affected, set)


class TestRunDeltaAnalysisNewRepoDiscovery:
    """AC6: run_delta_analysis integrates new repo discovery."""

    def test_stub_removed(self):
        """AC6: The 'not yet implemented' stub is replaced with real discovery logic."""
        import inspect
        source = inspect.getsource(DependencyMapService.run_delta_analysis)
        assert "not yet implemented" not in source, (
            "The 'not yet implemented' stub must be replaced with real discovery logic"
        )

    def test_discover_called_when_new_repos_detected(self, tmp_path):
        """AC6: run_delta_analysis calls _discover_and_assign_new_repos for new repos."""
        svc = _make_service(tmp_path)

        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)
        (depmap_dir / "_index.md").write_text(
            "---\nschema_version: 1.0\n---\n\n"
            "## Repo-to-Domain Matrix\n\n"
            "| Repository | Domain |\n|---|---|\n"
        )
        (depmap_dir / "_domains.json").write_text(json.dumps([
            {"name": "auth", "description": "Auth", "participating_repos": []},
        ]))

        new_repo = {"alias": "brand-new", "clone_path": str(tmp_path / "brand-new")}

        with patch.object(svc, "detect_changes", return_value=([], [new_repo], [])), \
             patch.object(svc, "identify_affected_domains",
                          return_value={"auth", "__NEW_REPO_DISCOVERY__"}), \
             patch.object(svc, "_discover_and_assign_new_repos",
                          return_value={"auth"}) as mock_discover, \
             patch.object(svc, "_update_affected_domains", return_value=[]), \
             patch.object(svc, "_reindex_cidx_meta"), \
             patch.object(svc, "_finalize_delta_tracking"), \
             patch.object(svc, "_get_activated_repos", return_value=[new_repo]):
            svc._analyzer.generate_claude_md.return_value = None
            svc.run_delta_analysis()

        mock_discover.assert_called_once()
        call_kwargs = mock_discover.call_args[1]
        assert "new_repos" in call_kwargs
        assert call_kwargs["new_repos"] == [new_repo]

    def test_new_repo_sentinel_removed_from_affected_domains(self, tmp_path):
        """AC6: __NEW_REPO_DISCOVERY__ sentinel is not passed to _update_affected_domains."""
        svc = _make_service(tmp_path)

        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)
        (depmap_dir / "_index.md").write_text(
            "---\nschema_version: 1.0\n---\n\n"
            "## Repo-to-Domain Matrix\n\n"
            "| Repository | Domain |\n|---|---|\n"
        )
        (depmap_dir / "_domains.json").write_text(json.dumps([
            {"name": "auth", "description": "Auth", "participating_repos": []},
        ]))

        new_repo = {"alias": "brand-new", "clone_path": str(tmp_path / "brand-new")}

        captured_domains = []

        def capture_update(affected_domains, *args, **kwargs):
            captured_domains.extend(list(affected_domains))
            return []

        with patch.object(svc, "detect_changes", return_value=([], [new_repo], [])), \
             patch.object(svc, "identify_affected_domains",
                          return_value={"auth", "__NEW_REPO_DISCOVERY__"}), \
             patch.object(svc, "_discover_and_assign_new_repos", return_value={"auth"}), \
             patch.object(svc, "_update_affected_domains", side_effect=capture_update), \
             patch.object(svc, "_reindex_cidx_meta"), \
             patch.object(svc, "_finalize_delta_tracking"), \
             patch.object(svc, "_get_activated_repos", return_value=[new_repo]):
            svc._analyzer.generate_claude_md.return_value = None
            svc.run_delta_analysis()

        assert "__NEW_REPO_DISCOVERY__" not in captured_domains, (
            "__NEW_REPO_DISCOVERY__ sentinel must not be passed to _update_affected_domains"
        )
