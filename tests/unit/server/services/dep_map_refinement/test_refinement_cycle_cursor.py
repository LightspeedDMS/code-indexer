"""
Unit tests for Story #359: Cursor batch selection and wrap-around.

Tests cover Component 5: Domain cycling with persistent cursor in run_refinement_cycle().

TDD RED PHASE: Tests written before production code exists.
"""

import json
from pathlib import Path
from unittest.mock import Mock


from .conftest import (
    FULL_DOMAIN_BODY,
    FULL_DOMAIN_CONTENT,
    SAMPLE_DOMAINS_JSON,
    make_config,
    make_dependency_map_dir,
    make_live_dep_map,
    make_service,
)


class TestCursorBatchSelection:
    """Domain cycling selects the correct N domains starting from cursor."""

    def test_cursor_batch_selection_from_start(self, tmp_path: Path):
        """
        Given 3 domains and cursor=0, domains_per_run=2
        When run_refinement_cycle() runs
        Then domains at index 0 (auth-domain) and 1 (data-pipeline) are processed.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        (dep_map / "_domains.json").write_text(json.dumps(SAMPLE_DOMAINS_JSON))
        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)
        (dep_map / "data-pipeline.md").write_text(FULL_DOMAIN_CONTENT)
        make_live_dep_map(tmp_path)

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement_file.return_value = FULL_DOMAIN_BODY
        mock_analyzer.build_new_domain_prompt.return_value = "new prompt"
        mock_analyzer._generate_index_md = Mock()

        service = make_service(
            tmp_path,
            mock_analyzer,
            make_config(refinement_enabled=True, refinement_domains_per_run=2),
            tracking_data={"refinement_cursor": 0},
        )

        service.run_refinement_cycle()

        assert mock_analyzer.build_refinement_prompt.call_count == 2
        calls = mock_analyzer.build_refinement_prompt.call_args_list
        called_domains = [c.kwargs.get("domain_name") or c.args[0] for c in calls]
        assert "auth-domain" in called_domains
        assert "data-pipeline" in called_domains

    def test_cursor_batch_selection_from_middle(self, tmp_path: Path):
        """
        Given 3 domains and cursor=1, domains_per_run=2
        When run_refinement_cycle() runs
        Then domains at index 1 (data-pipeline) and 2 (api-gateway) are processed.
        auth-domain (index 0) must NOT be processed.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        (dep_map / "_domains.json").write_text(json.dumps(SAMPLE_DOMAINS_JSON))
        (dep_map / "data-pipeline.md").write_text(FULL_DOMAIN_CONTENT)
        (dep_map / "api-gateway.md").write_text(FULL_DOMAIN_CONTENT)
        make_live_dep_map(tmp_path)

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement_file.return_value = FULL_DOMAIN_BODY
        mock_analyzer.build_new_domain_prompt.return_value = "new prompt"
        mock_analyzer._generate_index_md = Mock()

        service = make_service(
            tmp_path,
            mock_analyzer,
            make_config(refinement_enabled=True, refinement_domains_per_run=2),
            tracking_data={"refinement_cursor": 1},
        )

        service.run_refinement_cycle()

        calls = mock_analyzer.build_refinement_prompt.call_args_list
        called_domains = [c.kwargs.get("domain_name") or c.args[0] for c in calls]
        assert "data-pipeline" in called_domains
        assert "api-gateway" in called_domains
        assert "auth-domain" not in called_domains


class TestCursorWrapsAround:
    """Cursor wraps around when it reaches end of domain list."""

    def test_cursor_wraps_when_at_end(self, tmp_path: Path):
        """
        Given 3 domains and cursor=3 (past end), domains_per_run=1
        When run_refinement_cycle() runs
        Then cursor wraps to 0 and auth-domain (index 0) is processed.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        (dep_map / "_domains.json").write_text(json.dumps(SAMPLE_DOMAINS_JSON))
        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)
        make_live_dep_map(tmp_path)

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement_file.return_value = FULL_DOMAIN_BODY
        mock_analyzer.build_new_domain_prompt.return_value = "new prompt"
        mock_analyzer._generate_index_md = Mock()

        service = make_service(
            tmp_path,
            mock_analyzer,
            make_config(refinement_enabled=True, refinement_domains_per_run=1),
            tracking_data={"refinement_cursor": 3},
        )

        service.run_refinement_cycle()

        calls = mock_analyzer.build_refinement_prompt.call_args_list
        assert len(calls) >= 1
        called_domain = calls[0].kwargs.get("domain_name") or calls[0].args[0]
        assert called_domain == "auth-domain"

    def test_cursor_advanced_after_run(self, tmp_path: Path):
        """
        Given cursor=0, domains_per_run=2
        When run_refinement_cycle() runs
        Then tracking is updated with refinement_cursor=2.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        (dep_map / "_domains.json").write_text(json.dumps(SAMPLE_DOMAINS_JSON))
        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)
        (dep_map / "data-pipeline.md").write_text(FULL_DOMAIN_CONTENT)
        make_live_dep_map(tmp_path)

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement_file.return_value = FULL_DOMAIN_BODY
        mock_analyzer.build_new_domain_prompt.return_value = "new prompt"
        mock_analyzer._generate_index_md = Mock()

        service = make_service(
            tmp_path,
            mock_analyzer,
            make_config(refinement_enabled=True, refinement_domains_per_run=2),
            tracking_data={"refinement_cursor": 0},
        )

        service.run_refinement_cycle()

        update_calls = service._tracking_backend.update_tracking.call_args_list
        cursor_updates = [
            c for c in update_calls if "refinement_cursor" in (c.kwargs or {})
        ]
        assert len(cursor_updates) >= 1
        assert cursor_updates[-1].kwargs["refinement_cursor"] == 2


class TestEmptyDomainList:
    """run_refinement_cycle handles empty or missing domain list gracefully."""

    def test_empty_domains_json_no_error(self, tmp_path: Path):
        """
        Given _domains.json exists but contains an empty list
        When run_refinement_cycle() runs
        Then no error is raised and nothing is processed.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        (dep_map / "_domains.json").write_text("[]")

        mock_analyzer = Mock()
        config = make_config(refinement_enabled=True)
        service = make_service(tmp_path, mock_analyzer, config)

        service.run_refinement_cycle()  # Must not raise

        mock_analyzer.invoke_refinement_file.assert_not_called()

    def test_missing_domains_json_no_error(self, tmp_path: Path):
        """
        Given _domains.json does not exist
        When run_refinement_cycle() runs
        Then no error is raised.
        """
        make_dependency_map_dir(tmp_path)  # directory exists, no _domains.json

        mock_analyzer = Mock()
        config = make_config(refinement_enabled=True)
        service = make_service(tmp_path, mock_analyzer, config)

        service.run_refinement_cycle()  # Must not raise

        mock_analyzer.invoke_refinement_file.assert_not_called()
