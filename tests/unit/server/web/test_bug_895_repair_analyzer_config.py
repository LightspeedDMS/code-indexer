"""
Unit tests for Bug #895 — repair analyzer closure reads dependency_map_pass2_max_turns
from the correct nested ClaudeIntegrationConfig, not off top-level ServerConfig.

Tests:
  TestRepairAnalyzerHonorsPass2MaxTurns — closure uses config value, not hardcoded 25
  TestRepairAnalyzerLogsOnConfigFailure — config failure logs ERROR and returns False
"""

import logging

from unittest.mock import MagicMock

from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig
from code_indexer.server.web.dependency_map_routes import _build_domain_analyzer


class TestRepairAnalyzerHonorsPass2MaxTurns:
    """Bug #895 Site 1: analyzer closure must read max_turns from ClaudeIntegrationConfig."""

    def _make_dep_map_service(self, max_turns: int):
        """Return a fake dep_map_service whose _config_manager returns a known max_turns."""
        ci_config = ClaudeIntegrationConfig(dependency_map_pass2_max_turns=max_turns)

        config_manager = MagicMock()
        config_manager.get_claude_integration_config.return_value = ci_config

        analyzer_obj = MagicMock()
        analyzer_obj.run_pass_2_per_domain.return_value = None

        service = MagicMock()
        service._config_manager = config_manager
        service._analyzer = analyzer_obj
        service._activity_journal = None
        service._get_activated_repos.return_value = []
        service._enrich_repo_sizes.return_value = []
        return service, analyzer_obj

    def test_analyzer_uses_configured_max_turns(self, tmp_path):
        """Closure passes max_turns=77 (from config) not hardcoded 25."""
        service, analyzer_obj = self._make_dep_map_service(max_turns=77)

        # Simulate a non-empty domain file so closure returns True
        domain_name = "test-domain"
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        domain_file = out_dir / f"{domain_name}.md"
        domain_file.write_text("content")

        analyzer = _build_domain_analyzer(service, tmp_path)
        domain = {"name": domain_name}
        analyzer(out_dir, domain, [], [])

        # Assert run_pass_2_per_domain was called with max_turns=77
        assert analyzer_obj.run_pass_2_per_domain.called
        _, kwargs = analyzer_obj.run_pass_2_per_domain.call_args
        assert kwargs["max_turns"] == 77, (
            f"Expected max_turns=77 from config, got {kwargs['max_turns']}. "
            "Bug #895: closure reads from wrong object (ServerConfig instead of ClaudeIntegrationConfig)."
        )

    def test_analyzer_uses_500_turns_when_configured(self, tmp_path):
        """Closure passes max_turns=500 — matching the production failure scenario."""
        service, analyzer_obj = self._make_dep_map_service(max_turns=500)

        domain_name = "impl-mgmt"
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / f"{domain_name}.md").write_text("content")

        analyzer = _build_domain_analyzer(service, tmp_path)
        analyzer(out_dir, {"name": domain_name}, [], [])

        _, kwargs = analyzer_obj.run_pass_2_per_domain.call_args
        assert kwargs["max_turns"] == 500, (
            f"Expected max_turns=500, got {kwargs['max_turns']}. "
            "Bug #895 production scenario: user sets 500 via Web UI, code uses 25."
        )

    def test_analyzer_does_not_use_hardcoded_25(self, tmp_path):
        """Sanity: with config returning 77, value must not be 25 (old hardcoded default)."""
        service, analyzer_obj = self._make_dep_map_service(max_turns=77)

        domain_name = "domain-x"
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / f"{domain_name}.md").write_text("content")

        analyzer = _build_domain_analyzer(service, tmp_path)
        analyzer(out_dir, {"name": domain_name}, [], [])

        _, kwargs = analyzer_obj.run_pass_2_per_domain.call_args
        assert kwargs["max_turns"] != 25, (
            "Bug #895: closure is still using hardcoded 25 instead of config value."
        )


class TestRepairAnalyzerLogsOnConfigFailure:
    """Bug #895: config load failure must log ERROR and return False (not swallow silently)."""

    def _make_service_with_failing_config(self, exc: Exception):
        """Return a fake dep_map_service whose config_manager raises on get_claude_integration_config."""
        config_manager = MagicMock()
        config_manager.get_claude_integration_config.side_effect = exc

        analyzer_obj = MagicMock()

        service = MagicMock()
        service._config_manager = config_manager
        service._analyzer = analyzer_obj
        service._activity_journal = None
        service._get_activated_repos.return_value = []
        service._enrich_repo_sizes.return_value = []
        return service, analyzer_obj

    def test_config_failure_returns_false(self, tmp_path):
        """When get_claude_integration_config raises, closure returns False."""
        service, _ = self._make_service_with_failing_config(
            RuntimeError("DB unavailable")
        )

        out_dir = tmp_path / "out"
        out_dir.mkdir()

        analyzer = _build_domain_analyzer(service, tmp_path)
        result = analyzer(out_dir, {"name": "some-domain"}, [], [])

        assert result is False, (
            "Bug #895: config failure must return False, not silently proceed."
        )

    def test_config_failure_emits_error_log(self, tmp_path, caplog):
        """When get_claude_integration_config raises, closure emits an ERROR log."""
        exc_message = "DB unavailable"
        service, _ = self._make_service_with_failing_config(RuntimeError(exc_message))

        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with caplog.at_level(logging.ERROR):
            analyzer = _build_domain_analyzer(service, tmp_path)
            analyzer(out_dir, {"name": "some-domain"}, [], [])

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, (
            "Bug #895: config failure must emit an ERROR log. "
            "Current code swallows the exception silently."
        )
        combined = " ".join(r.getMessage() for r in error_records)
        assert exc_message in combined, (
            f"ERROR log should contain exception message '{exc_message}', got: {combined}"
        )

    def test_config_failure_does_not_call_run_pass_2(self, tmp_path):
        """When config fails, run_pass_2_per_domain must not be called."""
        service, analyzer_obj = self._make_service_with_failing_config(
            ValueError("config broken")
        )

        out_dir = tmp_path / "out"
        out_dir.mkdir()

        analyzer = _build_domain_analyzer(service, tmp_path)
        analyzer(out_dir, {"name": "some-domain"}, [], [])

        analyzer_obj.run_pass_2_per_domain.assert_not_called()
