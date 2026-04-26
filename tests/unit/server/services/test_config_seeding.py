"""Tests for config seeding helper (Bug #678).

Tests validate that seed_provider_config() correctly overlays server-side
provider config onto CLI subprocess config.json before each cidx index launch.
"""

import json
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest


class TestConfigSeedingNoOp:
    def test_seed_noop_if_config_absent(self, tmp_path):
        """No .code-indexer/config.json present — must not raise."""
        from code_indexer.server.services.config_seeding import seed_provider_config

        seed_provider_config(str(tmp_path))  # no .code-indexer/config.json — no error

    def test_seed_noop_if_code_indexer_dir_absent(self, tmp_path):
        """No .code-indexer/ directory at all — must not raise."""
        from code_indexer.server.services.config_seeding import seed_provider_config

        seed_provider_config(str(tmp_path / "nonexistent_repo"))


class TestConfigSeedingOverlays:
    @pytest.fixture
    def repo_dir(self, tmp_path):
        ci_dir = tmp_path / ".code-indexer"
        ci_dir.mkdir()
        config = {
            "voyage_ai": {"timeout": 999},
            "cohere": {"timeout": 999},
            "custom_key": "preserve_me",
        }
        (ci_dir / "config.json").write_text(json.dumps(config))
        return str(tmp_path)

    def test_seed_overlays_voyage_timeout(self, repo_dir):
        """Server default VoyageAIConfig.timeout=30 replaces disk value 999."""
        from code_indexer.server.services.config_seeding import seed_provider_config

        seed_provider_config(repo_dir)
        config = json.loads(
            (Path(repo_dir) / ".code-indexer" / "config.json").read_text()
        )
        assert config["voyage_ai"]["timeout"] == 30

    def test_seed_overlays_cohere_timeout(self, repo_dir):
        """Server default CohereConfig.timeout=30 replaces disk value 999."""
        from code_indexer.server.services.config_seeding import seed_provider_config

        seed_provider_config(repo_dir)
        config = json.loads(
            (Path(repo_dir) / ".code-indexer" / "config.json").read_text()
        )
        assert config["cohere"]["timeout"] == 30

    def test_seed_overlays_nested_health_monitor_keys(self, repo_dir):
        """Nested health_monitor sub-keys are seeded correctly."""
        from code_indexer.server.services.config_seeding import seed_provider_config

        seed_provider_config(repo_dir)
        config = json.loads(
            (Path(repo_dir) / ".code-indexer" / "config.json").read_text()
        )
        assert config["voyage_ai"]["health_monitor"]["rolling_window_minutes"] == 60
        assert config["voyage_ai"]["health_monitor"]["down_consecutive_failures"] == 5

    def test_seed_overlays_cohere_nested_health_monitor_keys(self, repo_dir):
        """Cohere health_monitor sub-keys are seeded correctly."""
        from code_indexer.server.services.config_seeding import seed_provider_config

        seed_provider_config(repo_dir)
        config = json.loads(
            (Path(repo_dir) / ".code-indexer" / "config.json").read_text()
        )
        assert config["cohere"]["health_monitor"]["rolling_window_minutes"] == 60
        assert config["cohere"]["health_monitor"]["down_consecutive_failures"] == 5

    def test_seed_reranker_timeout_set(self, repo_dir):
        """Reranker timeouts from both providers are seeded correctly."""
        from code_indexer.server.services.config_seeding import seed_provider_config

        seed_provider_config(repo_dir)
        config = json.loads(
            (Path(repo_dir) / ".code-indexer" / "config.json").read_text()
        )
        assert config["voyage_ai"]["reranker_timeout"] == 15.0
        assert config["cohere"]["reranker_timeout"] == 15.0

    def test_seed_preserves_non_seeded_keys(self, repo_dir):
        """Keys not in SEEDED_KEYS must remain untouched."""
        from code_indexer.server.services.config_seeding import seed_provider_config

        seed_provider_config(repo_dir)
        config = json.loads(
            (Path(repo_dir) / ".code-indexer" / "config.json").read_text()
        )
        assert config["custom_key"] == "preserve_me"


class TestConfigSeedingNoSinbinKeys:
    def test_seed_does_NOT_contain_sinbin_keys(self):
        """SEEDED_KEYS must not contain any sinbin-related keys."""
        from code_indexer.server.services.config_seeding import SEEDED_KEYS

        for key in SEEDED_KEYS:
            assert "sinbin" not in key, f"Sinbin key found in SEEDED_KEYS: {key}"


class TestConfigSeedingErrorHandling:
    def test_seed_handles_malformed_json(self, tmp_path):
        """Malformed JSON in config.json must not raise — logs warning instead."""
        from code_indexer.server.services.config_seeding import seed_provider_config

        ci_dir = tmp_path / ".code-indexer"
        ci_dir.mkdir()
        (ci_dir / "config.json").write_text("NOT VALID JSON {{{")
        seed_provider_config(str(tmp_path))  # should not raise

    def test_seed_atomic_write_on_rename_failure(self, tmp_path):
        """When os.rename fails, original file content must be preserved."""
        from code_indexer.server.services.config_seeding import seed_provider_config

        ci_dir = tmp_path / ".code-indexer"
        ci_dir.mkdir()
        original_content = json.dumps(
            {"voyage_ai": {"timeout": 999}, "custom_key": "keep"}
        )
        config_file = ci_dir / "config.json"
        config_file.write_text(original_content)

        with patch(
            "code_indexer.server.services.config_seeding.os.replace",
            side_effect=OSError("rename failed"),
        ):
            seed_provider_config(str(tmp_path))

        # Original file must be unchanged
        after = config_file.read_text()
        assert after == original_content

    def test_seed_cleans_up_tmp_file_on_rename_failure(self, tmp_path):
        """Temp file must be deleted when atomic rename fails."""
        from code_indexer.server.services.config_seeding import seed_provider_config

        ci_dir = tmp_path / ".code-indexer"
        ci_dir.mkdir()
        (ci_dir / "config.json").write_text(json.dumps({"voyage_ai": {"timeout": 10}}))

        with patch(
            "code_indexer.server.services.config_seeding.os.replace",
            side_effect=OSError("rename failed"),
        ):
            seed_provider_config(str(tmp_path))

        # No leftover .tmp files
        tmp_files = list(ci_dir.glob("*.tmp"))
        assert tmp_files == [], f"Leftover tmp files: {tmp_files}"


class TestDotPathHelpers:
    def test_resolve_dot_path_simple(self):
        from code_indexer.server.services.config_seeding import _resolve_dot_path

        data = {"a": {"b": 42}}
        assert _resolve_dot_path(data, "a.b") == 42

    def test_resolve_dot_path_missing_key_returns_none(self):
        from code_indexer.server.services.config_seeding import _resolve_dot_path

        data: dict[str, object] = {"a": {}}
        assert _resolve_dot_path(data, "a.b") is None

    def test_resolve_dot_path_top_level(self):
        from code_indexer.server.services.config_seeding import _resolve_dot_path

        data = {"key": "value"}
        assert _resolve_dot_path(data, "key") == "value"

    def test_resolve_dot_path_none_on_non_dict_intermediate(self):
        from code_indexer.server.services.config_seeding import _resolve_dot_path

        data = {"a": "not_a_dict"}
        assert _resolve_dot_path(data, "a.b") is None

    def test_set_dot_path_simple(self):
        from code_indexer.server.services.config_seeding import _set_dot_path

        data: dict = {}
        _set_dot_path(data, "a.b", 99)
        assert data == {"a": {"b": 99}}

    def test_set_dot_path_creates_intermediate_dicts(self):
        from code_indexer.server.services.config_seeding import _set_dot_path

        data: dict = {}
        _set_dot_path(data, "x.y.z", "deep")
        assert data["x"]["y"]["z"] == "deep"

    def test_set_dot_path_overwrites_existing(self):
        from code_indexer.server.services.config_seeding import _set_dot_path

        data = {"a": {"b": "old"}}
        _set_dot_path(data, "a.b", "new")
        assert data["a"]["b"] == "new"

    def test_set_dot_path_replaces_non_dict_intermediate(self):
        from code_indexer.server.services.config_seeding import _set_dot_path

        data: dict[str, object] = {"a": "scalar"}
        _set_dot_path(data, "a.b", 1)
        # _set_dot_path replaces the "a" entry from str to dict at runtime;
        # cast tells mypy the entry is now a dict so ["b"] is valid.
        assert cast(dict[str, object], data["a"])["b"] == 1
