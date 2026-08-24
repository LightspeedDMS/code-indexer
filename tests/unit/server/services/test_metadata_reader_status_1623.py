"""
Unit tests for metadata_reader.read_status() (Bug #1623).

RED PHASE: read_status() does not exist yet in production code.

Bug #1623: RefreshScheduler._check_stale_index_metadata()'s
in_progress/failed interrupted-index signal only ever read the legacy bare
metadata.json for the `status` field, even though Bug #1591 already made
the sibling `current_commit` signal provider-aware via
read_current_commit(). These tests assert that read_status() exists and
follows the IDENTICAL provider-first, legacy-fallback precedence as
read_current_commit() (same file names, same "malformed/missing key stops
at that file rather than falling back" contract).
"""

import json
from pathlib import Path

import pytest

from code_indexer.server.services.metadata_reader import read_status


@pytest.fixture
def code_indexer_dir(tmp_path: Path) -> Path:
    """Create and return .code-indexer directory inside tmp_path."""
    d = tmp_path / ".code-indexer"
    d.mkdir()
    return d


class TestReadStatusVoyageFile:
    """Provider-suffixed file (metadata-voyage-ai.json) is present."""

    def test_returns_status_from_voyage_file(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"status": "in_progress"})
        )

        assert read_status(tmp_path) == "in_progress"

    def test_prefers_voyage_over_legacy_when_both_present(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """Reproduces the real colorama/markupsafe fleet scenario: the
        provider-suffixed file disagrees with the legacy file, and the
        provider file must win."""
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"status": "in_progress"})
        )
        (code_indexer_dir / "metadata.json").write_text(
            json.dumps({"status": "completed"})
        )

        assert read_status(tmp_path) == "in_progress"

    def test_accepts_string_path(self, tmp_path: Path, code_indexer_dir: Path) -> None:
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"status": "completed"})
        )

        assert read_status(str(tmp_path)) == "completed"


class TestReadStatusLegacyFallback:
    """Only legacy metadata.json is present (migration safety)."""

    def test_returns_status_from_legacy_file_when_voyage_absent(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        (code_indexer_dir / "metadata.json").write_text(
            json.dumps({"status": "failed"})
        )

        assert read_status(tmp_path) == "failed"


class TestReadStatusMissingFiles:
    def test_returns_none_when_neither_file_exists(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        assert read_status(tmp_path) is None

    def test_returns_none_when_code_indexer_dir_missing(self, tmp_path: Path) -> None:
        assert read_status(tmp_path) is None


class TestReadStatusMalformedMetadata:
    def test_returns_none_on_malformed_voyage_json(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            "{ this is not valid json }"
        )

        assert read_status(tmp_path) is None

    def test_malformed_voyage_json_does_not_fall_back_to_legacy(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """Matches read_current_commit's documented contract: a malformed
        provider file returns None immediately, WITHOUT consulting the
        legacy file, even when the legacy file has a perfectly valid
        status."""
        (code_indexer_dir / "metadata-voyage-ai.json").write_text("NOT JSON")
        (code_indexer_dir / "metadata.json").write_text(
            json.dumps({"status": "failed"})
        )

        assert read_status(tmp_path) is None

    def test_returns_none_on_malformed_legacy_json(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        (code_indexer_dir / "metadata.json").write_text("NOT JSON")

        assert read_status(tmp_path) is None


class TestReadStatusInvalidStatusValues:
    def test_returns_none_when_status_key_missing(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"current_commit": "abc123"})
        )

        assert read_status(tmp_path) is None

    def test_missing_voyage_status_does_not_fall_back_to_valid_legacy_status(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """Same "stop at the provider file" contract as the malformed-JSON
        case above, but for a provider file that parses fine yet has no
        usable status key -- must NOT fall back to a legacy file that does
        have a valid status."""
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"current_commit": "abc123"})
        )
        (code_indexer_dir / "metadata.json").write_text(
            json.dumps({"status": "in_progress"})
        )

        assert read_status(tmp_path) is None

    def test_returns_none_when_status_is_empty_string(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"status": ""})
        )

        assert read_status(tmp_path) is None
