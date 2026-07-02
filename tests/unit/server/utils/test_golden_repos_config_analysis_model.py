"""
Tests for GoldenReposConfig.analysis_model field (Story #76 - AC2).

Verifies that GoldenReposConfig dataclass includes analysis_model field
with correct default value and persistence behavior.
"""

from src.code_indexer.server.utils.config_manager import GoldenReposConfig


class TestGoldenReposConfigAnalysisModel:
    """Test suite for GoldenReposConfig analysis_model field."""

    def test_analysis_model_default_value_opus(self):
        """AC2: analysis_model defaults to 'opus'."""
        config = GoldenReposConfig()
        assert config.analysis_model == "opus"

    def test_analysis_model_accepts_opus(self):
        """AC2: Can set analysis_model to 'opus'."""
        config = GoldenReposConfig(analysis_model="opus")
        assert config.analysis_model == "opus"

    def test_analysis_model_accepts_sonnet(self):
        """AC2: Can set analysis_model to 'sonnet'."""
        config = GoldenReposConfig(analysis_model="sonnet")
        assert config.analysis_model == "sonnet"

    def test_analysis_model_with_refresh_interval(self):
        """AC2: analysis_model coexists with existing refresh_interval_seconds field."""
        config = GoldenReposConfig(
            refresh_interval_seconds=7200, analysis_model="sonnet"
        )
        assert config.refresh_interval_seconds == 7200
        assert config.analysis_model == "sonnet"

    def test_analysis_model_dataclass_conversion(self):
        """AC2: Verify dataclass conversion to dict includes analysis_model."""
        from dataclasses import asdict

        config = GoldenReposConfig(analysis_model="sonnet")
        config_dict = asdict(config)

        assert "analysis_model" in config_dict
        assert config_dict["analysis_model"] == "sonnet"
