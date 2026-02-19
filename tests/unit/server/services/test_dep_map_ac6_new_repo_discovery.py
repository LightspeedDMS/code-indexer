"""
Unit tests for AC6: New repo discovery in delta refresh.

Story #216 AC6:
- When new repos are detected, _discover_and_assign_new_repos is called
- build_domain_discovery_prompt is invoked with new_repos and existing_domains
- _domains.json is updated with new repo assignments
- Affected domains are returned for re-analysis
- run_delta_analysis replaces the stub with real discovery logic

Bug fixes:
- Bug 1: Delta refresh creates new domains when Claude assigns to unknown domain
- Bug 2: Write failure is signalled so tracking does not finalize new repos
- Bug 3: identify_affected_domains() drops __NEW_REPO_DISCOVERY__ sentinel when _index.md is missing
"""

import json
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
        """AC6: Method returns (affected, write_success) with set of affected domain names."""
        svc = _make_service(tmp_path)
        depmap_dir = _setup_depmap_dir(tmp_path)

        new_repos = [{"alias": "new-svc", "clone_path": str(tmp_path / "new-svc")}]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value='[{"repo": "new-svc", "domains": ["auth"]}]'):
            affected, _ = svc._discover_and_assign_new_repos(
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
            affected, _ = svc._discover_and_assign_new_repos(
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
                          return_value=({"auth"}, True)) as mock_discover, \
             patch.object(svc, "_update_affected_domains", return_value=[]), \
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
             patch.object(svc, "_discover_and_assign_new_repos", return_value=({"auth"}, True)), \
             patch.object(svc, "_update_affected_domains", side_effect=capture_update), \
             patch.object(svc, "_finalize_delta_tracking"), \
             patch.object(svc, "_get_activated_repos", return_value=[new_repo]):
            svc._analyzer.generate_claude_md.return_value = None
            svc.run_delta_analysis()

        assert "__NEW_REPO_DISCOVERY__" not in captured_domains, (
            "__NEW_REPO_DISCOVERY__ sentinel must not be passed to _update_affected_domains"
        )


class TestBug1NewDomainCreation:
    """Bug 1: _discover_and_assign_new_repos creates new domains when Claude assigns unknown domain."""

    def test_assigns_repo_to_new_domain_when_domain_not_in_domains_json(self, tmp_path):
        """Bug 1: When Claude returns a domain name not in _domains.json, a new domain is created."""
        svc = _make_service(tmp_path)
        depmap_dir = _setup_depmap_dir(tmp_path)  # only 'auth' domain exists

        new_repos = [{"alias": "payments-svc", "clone_path": str(tmp_path / "payments-svc")}]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value='[{"repo": "payments-svc", "domains": ["payments"]}]'):
            svc._discover_and_assign_new_repos(
                new_repos=new_repos,
                existing_domains=["auth"],
                dependency_map_dir=depmap_dir,
                config=Mock(dependency_map_delta_max_turns=5, dependency_map_pass_timeout_seconds=120),
            )

        updated = json.loads((depmap_dir / "_domains.json").read_text())
        domain_names = [d["name"] for d in updated]
        assert "payments" in domain_names, (
            "New domain 'payments' should have been created in _domains.json"
        )

    def test_new_domain_has_correct_structure(self, tmp_path):
        """Bug 1: Newly created domain has name, empty description, participating_repos, empty evidence."""
        svc = _make_service(tmp_path)
        depmap_dir = _setup_depmap_dir(tmp_path)

        new_repos = [{"alias": "payments-svc", "clone_path": str(tmp_path / "payments-svc")}]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value='[{"repo": "payments-svc", "domains": ["payments"]}]'):
            svc._discover_and_assign_new_repos(
                new_repos=new_repos,
                existing_domains=["auth"],
                dependency_map_dir=depmap_dir,
                config=Mock(dependency_map_delta_max_turns=5, dependency_map_pass_timeout_seconds=120),
            )

        updated = json.loads((depmap_dir / "_domains.json").read_text())
        new_domain = next((d for d in updated if d["name"] == "payments"), None)
        assert new_domain is not None, "New 'payments' domain must exist"
        assert new_domain["name"] == "payments"
        assert new_domain["description"] == ""
        assert "payments-svc" in new_domain["participating_repos"]
        assert new_domain.get("evidence", "") == ""

    def test_new_domain_appears_in_affected_set(self, tmp_path):
        """Bug 1: New domain name is included in the returned affected set."""
        svc = _make_service(tmp_path)
        depmap_dir = _setup_depmap_dir(tmp_path)

        new_repos = [{"alias": "payments-svc", "clone_path": str(tmp_path / "payments-svc")}]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value='[{"repo": "payments-svc", "domains": ["payments"]}]'):
            affected, write_success = svc._discover_and_assign_new_repos(
                new_repos=new_repos,
                existing_domains=["auth"],
                dependency_map_dir=depmap_dir,
                config=Mock(dependency_map_delta_max_turns=5, dependency_map_pass_timeout_seconds=120),
            )

        assert "payments" in affected, (
            "Newly created domain 'payments' must be in the affected set for re-analysis"
        )

    def test_existing_domain_assignments_preserved_alongside_new_domain(self, tmp_path):
        """Bug 1: Existing domain assignments preserved when a new domain is also created."""
        svc = _make_service(tmp_path)
        depmap_dir = _setup_depmap_dir(tmp_path)

        new_repos = [
            {"alias": "existing-client", "clone_path": str(tmp_path / "existing-client")},
            {"alias": "payments-svc", "clone_path": str(tmp_path / "payments-svc")},
        ]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value=json.dumps([
                              {"repo": "existing-client", "domains": ["auth"]},
                              {"repo": "payments-svc", "domains": ["payments"]},
                          ])):
            affected, write_success = svc._discover_and_assign_new_repos(
                new_repos=new_repos,
                existing_domains=["auth"],
                dependency_map_dir=depmap_dir,
                config=Mock(dependency_map_delta_max_turns=5, dependency_map_pass_timeout_seconds=120),
            )

        updated = json.loads((depmap_dir / "_domains.json").read_text())
        auth_domain = next((d for d in updated if d["name"] == "auth"), None)
        payments_domain = next((d for d in updated if d["name"] == "payments"), None)

        assert auth_domain is not None
        assert "existing-client" in auth_domain["participating_repos"]
        assert payments_domain is not None
        assert "payments-svc" in payments_domain["participating_repos"]
        assert "auth" in affected
        assert "payments" in affected


class TestBug2WriteFailureTracking:
    """Bug 2: _discover_and_assign_new_repos returns write_success=False on write failure."""

    def test_returns_tuple_on_success(self, tmp_path):
        """Bug 2: Method returns (affected: Set[str], write_success: bool) tuple on success."""
        svc = _make_service(tmp_path)
        depmap_dir = _setup_depmap_dir(tmp_path)

        new_repos = [{"alias": "new-svc", "clone_path": str(tmp_path / "new-svc")}]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value='[{"repo": "new-svc", "domains": ["auth"]}]'):
            result = svc._discover_and_assign_new_repos(
                new_repos=new_repos,
                existing_domains=["auth"],
                dependency_map_dir=depmap_dir,
                config=Mock(dependency_map_delta_max_turns=5, dependency_map_pass_timeout_seconds=120),
            )

        assert isinstance(result, tuple), "Method must return a tuple"
        assert len(result) == 2, "Tuple must have exactly 2 elements"
        affected, write_success = result
        assert isinstance(affected, set), "First element must be a set"
        assert write_success is True, "write_success must be True on successful write"

    def test_returns_write_failure_signal_when_write_fails(self, tmp_path):
        """Bug 2: When _domains.json write fails, method returns write_success=False."""
        svc = _make_service(tmp_path)
        depmap_dir = _setup_depmap_dir(tmp_path)

        new_repos = [{"alias": "new-svc", "clone_path": str(tmp_path / "new-svc")}]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value='[{"repo": "new-svc", "domains": ["auth"]}]'), \
             patch("pathlib.Path.write_text", side_effect=OSError("Disk full")):
            result = svc._discover_and_assign_new_repos(
                new_repos=new_repos,
                existing_domains=["auth"],
                dependency_map_dir=depmap_dir,
                config=Mock(dependency_map_delta_max_turns=5, dependency_map_pass_timeout_seconds=120),
            )

        assert isinstance(result, tuple), "Must return tuple even on write failure"
        affected, write_success = result
        assert write_success is False, "write_success must be False when write fails"

    def test_run_delta_analysis_does_not_finalize_new_repos_when_write_fails(self, tmp_path):
        """Bug 2: When discovery write fails, new repos are not included in tracking finalization."""
        svc = _make_service(tmp_path)

        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)
        (depmap_dir / "_domains.json").write_text(json.dumps([
            {"name": "auth", "description": "Auth", "participating_repos": []},
        ]))

        new_repo = {"alias": "brand-new", "clone_path": str(tmp_path / "brand-new")}
        changed_repo = {"alias": "existing-repo", "clone_path": str(tmp_path / "existing-repo")}

        finalize_calls = []

        def capture_finalize(config, all_repos):
            finalize_calls.append(list(all_repos) if all_repos else [])

        with patch.object(svc, "detect_changes",
                          return_value=([changed_repo], [new_repo], [])), \
             patch.object(svc, "identify_affected_domains",
                          return_value={"auth", "__NEW_REPO_DISCOVERY__"}), \
             patch.object(svc, "_discover_and_assign_new_repos",
                          return_value=({"auth"}, False)), \
             patch.object(svc, "_update_affected_domains", return_value=[]), \
             patch.object(svc, "_finalize_delta_tracking", side_effect=capture_finalize), \
             patch.object(svc, "_get_activated_repos",
                          return_value=[new_repo, changed_repo]):
            svc._analyzer.generate_claude_md.return_value = None
            svc.run_delta_analysis()

        assert len(finalize_calls) == 1, "_finalize_delta_tracking must be called exactly once"
        finalized_repos = finalize_calls[0]
        finalized_aliases = [r.get("alias") for r in finalized_repos]
        assert "brand-new" not in finalized_aliases, (
            "New repo 'brand-new' must NOT be finalized when write failed"
        )
        assert "existing-repo" in finalized_aliases, (
            "Changed repo 'existing-repo' must still be finalized even if write failed"
        )

    def test_run_delta_analysis_finalizes_all_repos_when_write_succeeds(self, tmp_path):
        """Bug 2: When discovery write succeeds, all repos (including new) are finalized."""
        svc = _make_service(tmp_path)

        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)
        (depmap_dir / "_domains.json").write_text(json.dumps([
            {"name": "auth", "description": "Auth", "participating_repos": []},
        ]))

        new_repo = {"alias": "brand-new", "clone_path": str(tmp_path / "brand-new")}
        changed_repo = {"alias": "existing-repo", "clone_path": str(tmp_path / "existing-repo")}

        finalize_calls = []

        def capture_finalize(config, all_repos):
            finalize_calls.append(list(all_repos) if all_repos else [])

        with patch.object(svc, "detect_changes",
                          return_value=([changed_repo], [new_repo], [])), \
             patch.object(svc, "identify_affected_domains",
                          return_value={"auth", "__NEW_REPO_DISCOVERY__"}), \
             patch.object(svc, "_discover_and_assign_new_repos",
                          return_value=({"auth"}, True)), \
             patch.object(svc, "_update_affected_domains", return_value=[]), \
             patch.object(svc, "_finalize_delta_tracking", side_effect=capture_finalize), \
             patch.object(svc, "_get_activated_repos",
                          return_value=[new_repo, changed_repo]):
            svc._analyzer.generate_claude_md.return_value = None
            svc.run_delta_analysis()

        assert len(finalize_calls) == 1
        finalized_repos = finalize_calls[0]
        finalized_aliases = [r.get("alias") for r in finalized_repos]
        assert "brand-new" in finalized_aliases, (
            "New repo 'brand-new' MUST be finalized when write succeeded"
        )
        assert "existing-repo" in finalized_aliases, (
            "Changed repo 'existing-repo' must be finalized"
        )


class TestBug3MissingIndexMd:
    """Bug 3: identify_affected_domains() drops __NEW_REPO_DISCOVERY__ sentinel when _index.md is missing."""

    def test_identify_affected_domains_returns_sentinel_when_no_index_md_and_new_repos(
        self, tmp_path
    ):
        """Bug 3: When _index.md missing but new_repos non-empty, sentinel must be returned.

        Currently the method returns set() which causes run_delta_analysis() to early-return
        at line 1415 without ever reaching the __NEW_REPO_DISCOVERY__ check.
        """
        svc = _make_service(tmp_path)
        # Create dependency-map dir WITHOUT _index.md
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)
        # Deliberately do NOT create _index.md

        new_repo = {"alias": "brand-new", "clone_path": str(tmp_path / "brand-new")}

        result = svc.identify_affected_domains(
            changed_repos=[],
            new_repos=[new_repo],
            removed_repos=[],
        )

        assert "__NEW_REPO_DISCOVERY__" in result, (
            "When _index.md is missing but new_repos is non-empty, "
            "__NEW_REPO_DISCOVERY__ sentinel must be returned so run_delta_analysis "
            "can trigger domain discovery"
        )

    def test_identify_affected_domains_returns_empty_when_no_index_md_and_no_new_repos(
        self, tmp_path
    ):
        """Bug 3: When _index.md missing and no new repos, empty set is correct (preserve existing behavior)."""
        svc = _make_service(tmp_path)
        # Create dependency-map dir WITHOUT _index.md
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)
        # Deliberately do NOT create _index.md

        result = svc.identify_affected_domains(
            changed_repos=[],
            new_repos=[],
            removed_repos=[],
        )

        assert result == set(), (
            "When _index.md is missing and new_repos is empty, "
            "empty set should be returned (no work to do)"
        )


class TestBug4MissingDomainsJson:
    """Bug 4: _discover_and_assign_new_repos() should start with empty domain list when _domains.json missing."""

    def test_discover_assigns_new_repos_when_domains_json_missing(self, tmp_path):
        """Bug 4: When _domains.json doesn't exist, method should still process Claude's assignments
        and create new domains (not return early with 'cannot assign new repos')."""
        svc = _make_service(tmp_path)

        # Create dependency-map dir WITHOUT _domains.json
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)
        # Deliberately do NOT create _domains.json

        new_repos = [{"alias": "new-svc", "clone_path": str(tmp_path / "new-svc")}]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value='[{"repo": "new-svc", "domains": ["payments"]}]'):
            affected, write_success = svc._discover_and_assign_new_repos(
                new_repos=new_repos,
                existing_domains=["payments"],
                dependency_map_dir=depmap_dir,
                config=Mock(dependency_map_delta_max_turns=5, dependency_map_pass_timeout_seconds=120),
            )

        assert isinstance(affected, set), "Must return a set of affected domains"
        assert "payments" in affected, (
            "Domain 'payments' must be in affected set - method should not return early "
            "just because _domains.json was missing"
        )

    def test_discover_creates_domains_json_when_missing(self, tmp_path):
        """Bug 4: When _domains.json doesn't exist, the written _domains.json should contain
        the newly created domains from Claude's assignment."""
        svc = _make_service(tmp_path)

        # Create dependency-map dir WITHOUT _domains.json
        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)
        # Deliberately do NOT create _domains.json

        new_repos = [{"alias": "new-svc", "clone_path": str(tmp_path / "new-svc")}]
        svc._analyzer.build_domain_discovery_prompt.return_value = "discovery prompt"

        with patch.object(svc._analyzer, "invoke_domain_discovery",
                          return_value='[{"repo": "new-svc", "domains": ["payments"]}]'):
            svc._discover_and_assign_new_repos(
                new_repos=new_repos,
                existing_domains=["payments"],
                dependency_map_dir=depmap_dir,
                config=Mock(dependency_map_delta_max_turns=5, dependency_map_pass_timeout_seconds=120),
            )

        domains_file = depmap_dir / "_domains.json"
        assert domains_file.exists(), (
            "_domains.json must be created even when it didn't exist before"
        )
        written = json.loads(domains_file.read_text())
        domain_names = [d["name"] for d in written]
        assert "payments" in domain_names, (
            "Newly created domain 'payments' must be written to _domains.json"
        )
