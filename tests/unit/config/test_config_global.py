"""Tests for GlobalCliConfig loader (Story #690).

Tests use real filesystem via tmp_path and monkeypatch.setenv.
No mocks of file I/O — anti-mock (Messi Rule 01).
"""

import json
from pathlib import Path
from typing import Any, Tuple

import pytest

from code_indexer.config_global import (
    GlobalCliConfig,
    load_global_config,
    save_global_config,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _point_to_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect config path resolution to an isolated tmp file."""
    cfg_file = tmp_path / "global.json"
    monkeypatch.setenv("CIDX_GLOBAL_CONFIG_PATH", str(cfg_file))
    return cfg_file


def _write_config(cfg_file: Path, data: Any) -> None:
    """Write a JSON payload to cfg_file for setup purposes."""
    cfg_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_malformed_config(cfg_file: Path, content: str = "{not valid json") -> None:
    """Write malformed JSON content to cfg_file for error-path tests."""
    cfg_file.write_text(content, encoding="utf-8")


def _seed_and_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Tuple[Path, GlobalCliConfig]:
    """Point to tmp, load (triggers seed), return (cfg_file, config)."""
    cfg_file = _point_to_tmp(monkeypatch, tmp_path)
    cfg = load_global_config()
    return cfg_file, cfg


# ---------------------------------------------------------------------------
# Scenario: Default values  (parametrized to avoid copy-paste)
# ---------------------------------------------------------------------------

_EXPECTED_DEFAULTS = [
    ("voyage_reranker_model", "rerank-2.5"),
    ("cohere_reranker_model", "rerank-v3.5"),
    ("overfetch_multiplier", 5),
    ("auto_populate_rerank_query", True),
    ("preferred_vendor_order", ["voyage", "cohere"]),
]


@pytest.mark.parametrize("field,expected", _EXPECTED_DEFAULTS)
def test_default_rerank_field(
    field: str,
    expected: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Freshly seeded config has sensible defaults for every rerank field."""
    _point_to_tmp(monkeypatch, tmp_path)
    cfg = load_global_config()
    assert getattr(cfg.rerank, field) == expected


# ---------------------------------------------------------------------------
# Scenario: First-run auto-seed
# ---------------------------------------------------------------------------


class TestFirstRunAutoSeed:
    """When no file exists the loader creates it with defaults."""

    def test_file_created_on_first_load(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg_file = _point_to_tmp(monkeypatch, tmp_path)
        assert not cfg_file.exists()
        load_global_config()
        assert cfg_file.exists()

    def test_seeded_file_is_valid_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg_file, _ = _seed_and_load(monkeypatch, tmp_path)
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_seeded_file_has_indented_lines(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg_file, _ = _seed_and_load(monkeypatch, tmp_path)
        raw = cfg_file.read_text(encoding="utf-8")
        indented = [ln for ln in raw.splitlines() if ln.startswith("  ")]
        assert len(indented) > 0, "Expected indented lines in seeded JSON"

    def test_parent_dir_created_if_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        nested = tmp_path / "deep" / "nested" / "dir" / "global.json"
        monkeypatch.setenv("CIDX_GLOBAL_CONFIG_PATH", str(nested))
        assert not nested.parent.exists()
        load_global_config()
        assert nested.exists()


# ---------------------------------------------------------------------------
# Scenario: Respect XDG_CONFIG_HOME
# ---------------------------------------------------------------------------


class TestXdgConfigHome:
    """XDG_CONFIG_HOME override honored when CIDX_GLOBAL_CONFIG_PATH is not set."""

    def test_xdg_config_home_used_when_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("CIDX_GLOBAL_CONFIG_PATH", raising=False)
        xdg_dir = tmp_path / "custom_xdg"
        xdg_dir.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_dir))
        cfg = load_global_config()
        assert (xdg_dir / "cidx" / "global.json").exists()
        assert cfg.rerank.voyage_reranker_model == "rerank-2.5"

    def test_legacy_home_dotconfig_used_when_xdg_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("CIDX_GLOBAL_CONFIG_PATH", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        home_dir = tmp_path / "home" / "alice"
        home_dir.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home_dir))
        cfg = load_global_config()
        expected = home_dir / ".config" / "cidx" / "global.json"
        assert expected.exists()
        assert cfg.rerank.voyage_reranker_model == "rerank-2.5"


# ---------------------------------------------------------------------------
# Scenario: Hand-edited config survives reload
# ---------------------------------------------------------------------------


class TestHandEditedConfig:
    """User edits to the JSON file are respected on reload."""

    def test_hand_edited_value_survives_reload(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg_file, _ = _seed_and_load(monkeypatch, tmp_path)
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        data["rerank"]["auto_populate_rerank_query"] = False
        _write_config(cfg_file, data)
        cfg2 = load_global_config()
        assert cfg2.rerank.auto_populate_rerank_query is False

    def test_existing_file_is_not_overwritten_on_reload(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg_file, _ = _seed_and_load(monkeypatch, tmp_path)
        contents_after_seed = cfg_file.read_text(encoding="utf-8")
        load_global_config()
        assert cfg_file.read_text(encoding="utf-8") == contents_after_seed, (
            "load_global_config() must not overwrite an existing file on reload"
        )


# ---------------------------------------------------------------------------
# Scenario: Invalid JSON is rejected loudly
# ---------------------------------------------------------------------------


class TestInvalidJson:
    """Malformed JSON raises a clear error — no silent fallback."""

    def test_malformed_json_raises_with_path_info(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg_file = _point_to_tmp(monkeypatch, tmp_path)
        _write_malformed_config(cfg_file, "{not valid json")
        with pytest.raises(ValueError, match=str(cfg_file)):
            load_global_config()

    def test_malformed_json_does_not_silently_return_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg_file = _point_to_tmp(monkeypatch, tmp_path)
        _write_malformed_config(cfg_file, "{")
        with pytest.raises(ValueError):
            load_global_config()


# ---------------------------------------------------------------------------
# Scenario: Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    """Wrong types in the JSON raise a validation error."""

    def test_wrong_type_for_overfetch_multiplier_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg_file = _point_to_tmp(monkeypatch, tmp_path)
        _write_config(
            cfg_file,
            {
                "rerank": {
                    "voyage_reranker_model": "rerank-2.5",
                    "cohere_reranker_model": "rerank-v3.5",
                    "overfetch_multiplier": "not-an-int",
                    "auto_populate_rerank_query": True,
                    "preferred_vendor_order": ["voyage", "cohere"],
                }
            },
        )
        with pytest.raises((ValueError, TypeError)):
            load_global_config()


# ---------------------------------------------------------------------------
# Scenario: Loader is idempotent across multiple calls
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Calling load_global_config() multiple times returns consistent results."""

    def test_multiple_calls_return_same_values(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _point_to_tmp(monkeypatch, tmp_path)
        cfg1 = load_global_config()
        cfg2 = load_global_config()
        assert cfg1.rerank.voyage_reranker_model == cfg2.rerank.voyage_reranker_model
        assert cfg1.rerank.overfetch_multiplier == cfg2.rerank.overfetch_multiplier


# ---------------------------------------------------------------------------
# Scenario: save_global_config round-trip
# ---------------------------------------------------------------------------


class TestSaveRoundTrip:
    """save_global_config persists changes that load_global_config reads back."""

    def test_save_and_reload(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _point_to_tmp(monkeypatch, tmp_path)
        cfg = load_global_config()
        cfg.rerank.overfetch_multiplier = 10
        save_global_config(cfg)
        cfg2 = load_global_config()
        assert cfg2.rerank.overfetch_multiplier == 10

    def test_save_writes_nested_rerank_keys_in_sorted_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg_file, cfg = _seed_and_load(monkeypatch, tmp_path)
        save_global_config(cfg)
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        rerank_keys = list(data["rerank"].keys())
        assert len(rerank_keys) >= 3, "Expected at least 3 keys in rerank section"
        assert rerank_keys == sorted(rerank_keys), (
            f"rerank keys must be sorted, got: {rerank_keys}"
        )


# ---------------------------------------------------------------------------
# Scenario: Per-project config is untouched
# ---------------------------------------------------------------------------


class TestPerProjectConfigUntouched:
    """Global config loader never reads or writes .code-indexer/config.json."""

    def test_per_project_config_not_created_when_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _point_to_tmp(monkeypatch, tmp_path)
        load_global_config()
        assert not (tmp_path / ".code-indexer" / "config.json").exists()

    def test_pre_existing_per_project_config_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        per_project = tmp_path / ".code-indexer" / "config.json"
        per_project.parent.mkdir(parents=True)
        original = json.dumps({"project": "myproject", "version": 1}, indent=2)
        per_project.write_text(original, encoding="utf-8")
        _point_to_tmp(monkeypatch, tmp_path)
        load_global_config()
        assert per_project.exists()
        assert per_project.read_text(encoding="utf-8") == original
