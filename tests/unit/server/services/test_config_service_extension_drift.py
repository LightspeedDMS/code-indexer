"""Unit tests for Story #1001: ExtensionDrift dataclass and sync_repo_extensions_if_drifted().

Tests verify the complete behavior of sync_repo_extensions_if_drifted():
1. Returns ExtensionDrift(added, removed) when extensions drift
2. Returns None when already in sync or when repo config is absent/unreadable
3. Writes back updated extensions to repo config.json when drift detected
4. One-shot: second call after sync returns None (AC3)
"""

import json
from pathlib import Path
from typing import List, Set

import pytest

from code_indexer.server.services.config_service import ConfigService


# ---------------------------------------------------------------------------
# Fixtures and shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cs(tmp_path):
    """Real ConfigService backed by a fresh temp server directory."""
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    return ConfigService(server_dir_path=str(server_dir))


@pytest.fixture
def repo_dir(tmp_path):
    """Temp directory acting as golden repo root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _write_repo_config(repo_dir: Path, extensions: List[str]) -> None:
    """Write .code-indexer/config.json with given extension list (no leading dots)."""
    ci_dir = repo_dir / ".code-indexer"
    ci_dir.mkdir(exist_ok=True)
    (ci_dir / "config.json").write_text(json.dumps({"file_extensions": extensions}))


def _set_server_extensions(cs: ConfigService, extensions: List[str]) -> None:
    """Configure server indexable_extensions via the public API."""
    cs.update_setting("indexing", "indexable_extensions", extensions)


# ---------------------------------------------------------------------------
# Tests: ExtensionDrift dataclass
# ---------------------------------------------------------------------------


class TestExtensionDriftConstruction:
    """ExtensionDrift must be importable and store added/removed sets correctly."""

    @pytest.mark.parametrize(
        "added, removed",
        [
            ({"py", "js"}, set()),
            (set(), {"log", "tmp"}),
            ({"jsonl"}, {"log"}),
            (set(), set()),
        ],
    )
    def test_dataclass_stores_added_and_removed(
        self, added: Set[str], removed: Set[str]
    ):
        """ExtensionDrift must store added and removed sets as-is."""
        from code_indexer.server.services.config_service import ExtensionDrift

        drift = ExtensionDrift(added=added, removed=removed)
        assert drift.added == added
        assert drift.removed == removed


# ---------------------------------------------------------------------------
# Tests: returns None when no drift (or no config)
# ---------------------------------------------------------------------------


class TestSyncReturnsNoneWhenNoDrift:
    """sync_repo_extensions_if_drifted() returns None in all no-drift scenarios."""

    @pytest.mark.parametrize(
        "setup_label, server_exts, repo_exts",
        [
            ("matching_extensions", [".py", ".js", ".ts"], ["js", "py", "ts"]),
            ("both_empty", [], []),
        ],
    )
    def test_returns_none_when_extensions_match(
        self, cs, repo_dir, setup_label, server_exts, repo_exts
    ):
        """Returns None when server and repo extensions are identical."""
        _set_server_extensions(cs, server_exts)
        _write_repo_config(repo_dir, repo_exts)

        result = cs.sync_repo_extensions_if_drifted(str(repo_dir))
        assert result is None, f"[{setup_label}] Expected None but got {result}"

    def test_returns_none_when_indexing_config_is_none(self, cs, repo_dir):
        """Returns None when server indexing_config is None."""
        cs.get_config().indexing_config = None
        result = cs.sync_repo_extensions_if_drifted(str(repo_dir))
        assert result is None

    def test_returns_none_when_repo_config_file_absent(self, cs, repo_dir):
        """Returns None when .code-indexer/config.json does not exist."""
        _set_server_extensions(cs, [".py", ".js"])
        result = cs.sync_repo_extensions_if_drifted(str(repo_dir))
        assert result is None


# ---------------------------------------------------------------------------
# Tests: returns ExtensionDrift with correct content when drifted
# ---------------------------------------------------------------------------


class TestSyncReturnsDriftWhenExtensionsDiffer:
    """sync_repo_extensions_if_drifted() returns ExtensionDrift on extension mismatch."""

    @pytest.mark.parametrize(
        "server_exts, repo_exts, expected_added, expected_removed",
        [
            # Extension added to server
            ([".py", ".js", ".jsonl"], ["js", "py"], {"jsonl"}, set()),
            # Extension removed from server
            ([".py", ".js"], ["js", "log", "py"], set(), {"log"}),
            # Both added and removed
            ([".py", ".jsonl"], ["js", "py"], {"jsonl"}, {"js"}),
        ],
    )
    def test_drift_returns_correct_added_and_removed(
        self, cs, repo_dir, server_exts, repo_exts, expected_added, expected_removed
    ):
        """ExtensionDrift.added and .removed match the actual extension delta."""
        from code_indexer.server.services.config_service import ExtensionDrift

        _set_server_extensions(cs, server_exts)
        _write_repo_config(repo_dir, repo_exts)

        result = cs.sync_repo_extensions_if_drifted(str(repo_dir))

        assert isinstance(result, ExtensionDrift)
        assert result.added == expected_added, (
            f"added mismatch: {result.added} != {expected_added}"
        )
        assert result.removed == expected_removed, (
            f"removed mismatch: {result.removed} != {expected_removed}"
        )

    def test_drift_extension_names_have_no_leading_dots(self, cs, repo_dir):
        """Extension names in added/removed sets must not have leading dots."""
        _set_server_extensions(cs, [".py", ".jsonl"])
        _write_repo_config(repo_dir, ["js", "py"])

        result = cs.sync_repo_extensions_if_drifted(str(repo_dir))

        assert result is not None
        for ext in result.added | result.removed:
            assert not ext.startswith("."), (
                f"Extension '{ext}' must not have leading dot"
            )

    def test_drift_updates_repo_config_and_second_call_returns_none(self, cs, repo_dir):
        """Drift syncs config.json; second call returns None (AC3 one-shot behavior)."""
        _set_server_extensions(cs, [".py", ".js", ".jsonl"])
        _write_repo_config(repo_dir, ["js", "py"])

        first_result = cs.sync_repo_extensions_if_drifted(str(repo_dir))
        assert first_result is not None

        # Repo config must now contain jsonl
        written = json.loads((repo_dir / ".code-indexer" / "config.json").read_text())
        assert "jsonl" in set(written["file_extensions"])

        # Second call — extensions now match — must return None
        second_result = cs.sync_repo_extensions_if_drifted(str(repo_dir))
        assert second_result is None


# ---------------------------------------------------------------------------
# Tests: edge cases (malformed JSON, missing key)
# ---------------------------------------------------------------------------


class TestSyncEdgeCases:
    """Edge cases: malformed JSON, missing file_extensions key in repo config."""

    def test_malformed_json_returns_none_without_raising(self, cs, repo_dir):
        """Malformed JSON in repo config.json must return None, never raise."""
        _set_server_extensions(cs, [".py"])
        ci_dir = repo_dir / ".code-indexer"
        ci_dir.mkdir(exist_ok=True)
        (ci_dir / "config.json").write_text("NOT VALID JSON {{{")

        result = cs.sync_repo_extensions_if_drifted(str(repo_dir))
        assert result is None

    def test_missing_file_extensions_key_treated_as_empty_list(self, cs, repo_dir):
        """config.json without file_extensions key is treated as empty list."""
        from code_indexer.server.services.config_service import ExtensionDrift

        _set_server_extensions(cs, [".py"])
        ci_dir = repo_dir / ".code-indexer"
        ci_dir.mkdir(exist_ok=True)
        (ci_dir / "config.json").write_text(json.dumps({"other_key": "value"}))

        result = cs.sync_repo_extensions_if_drifted(str(repo_dir))

        assert isinstance(result, ExtensionDrift)
        assert "py" in result.added
