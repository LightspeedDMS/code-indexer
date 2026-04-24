"""
Unit tests for metadata_reader.read_current_commit() (Bug #890).

RED PHASE: Tests written before production code exists.
Asserts provider-aware metadata reading with legacy fallback.
"""

import json
from pathlib import Path

import pytest

from code_indexer.server.services.metadata_reader import read_current_commit


@pytest.fixture
def code_indexer_dir(tmp_path: Path) -> Path:
    """Create and return .code-indexer directory inside tmp_path."""
    d = tmp_path / ".code-indexer"
    d.mkdir()
    return d


class TestReadCurrentCommitVoyageFile:
    """Provider-suffixed file (metadata-voyage-ai.json) is present."""

    def test_returns_sha_from_voyage_file(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """Voyage file present -> return real SHA string."""
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"current_commit": "abc123sha"})
        )

        assert read_current_commit(tmp_path) == "abc123sha"

    def test_prefers_voyage_over_legacy_when_both_present(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """Voyage file wins over legacy file when both present."""
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"current_commit": "voyage-sha"})
        )
        (code_indexer_dir / "metadata.json").write_text(
            json.dumps({"current_commit": "legacy-sha"})
        )

        assert read_current_commit(tmp_path) == "voyage-sha"

    def test_accepts_string_path(self, tmp_path: Path, code_indexer_dir: Path) -> None:
        """Caller may pass str clone_path; function accepts both str and Path."""
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"current_commit": "str-path-sha"})
        )

        assert read_current_commit(str(tmp_path)) == "str-path-sha"


class TestReadCurrentCommitLegacyFallback:
    """Only legacy metadata.json is present (migration safety)."""

    def test_returns_sha_from_legacy_file_when_voyage_absent(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """Legacy-only path -> return real SHA from legacy file."""
        (code_indexer_dir / "metadata.json").write_text(
            json.dumps({"current_commit": "legacy-only-sha"})
        )

        assert read_current_commit(tmp_path) == "legacy-only-sha"


class TestReadCurrentCommitMissingFiles:
    """No metadata files or directory present."""

    def test_returns_none_when_neither_file_exists(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """No metadata files at all -> None."""
        assert read_current_commit(tmp_path) is None

    def test_returns_none_when_code_indexer_dir_missing(self, tmp_path: Path) -> None:
        """No .code-indexer directory -> None (not crash)."""
        assert read_current_commit(tmp_path) is None


class TestReadCurrentCommitMalformedMetadata:
    """Malformed JSON in metadata files."""

    def test_returns_none_on_malformed_voyage_json(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """Malformed JSON in voyage file -> None (no crash)."""
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            "{ this is not valid json }"
        )

        assert read_current_commit(tmp_path) is None

    def test_returns_none_on_malformed_legacy_json(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """Malformed JSON in legacy file -> None (no crash)."""
        (code_indexer_dir / "metadata.json").write_text("NOT JSON")

        assert read_current_commit(tmp_path) is None

    def test_returns_none_on_unicode_decode_error(self, tmp_path: Path) -> None:
        """Corrupted UTF-8 bytes in metadata file should return None, not crash."""
        code_indexer_dir = tmp_path / ".code-indexer"
        code_indexer_dir.mkdir()
        # Write invalid UTF-8 bytes
        (code_indexer_dir / "metadata-voyage-ai.json").write_bytes(b"\xff\xfe\xfd\xfc")
        result = read_current_commit(tmp_path)
        assert result is None


class TestReadCurrentCommitInvalidCommitValues:
    """current_commit key absent or empty."""

    def test_returns_none_when_current_commit_key_missing(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """Voyage file present but current_commit key absent -> None."""
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"indexed_at": "2024-01-01", "provider": "voyage-ai"})
        )

        assert read_current_commit(tmp_path) is None

    def test_returns_none_when_current_commit_is_empty_string(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """current_commit = '' (empty string) -> None (falsy, not a real SHA)."""
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"current_commit": ""})
        )

        assert read_current_commit(tmp_path) is None

    def test_returns_none_when_legacy_key_missing(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """Legacy file present but current_commit key absent -> None."""
        (code_indexer_dir / "metadata.json").write_text(
            json.dumps({"indexed_at": "2024-01-01"})
        )

        assert read_current_commit(tmp_path) is None
