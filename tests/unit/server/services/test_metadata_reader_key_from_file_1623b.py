"""
Unit tests for Bug #1623-B: collapse the near-duplicate
_read_status_from_file() / _read_commit_from_file() helpers in
metadata_reader.py into one shared _read_key_from_file(metadata_path, key)
helper (Messi Rule #4, anti-duplication -- the two ~30-line functions
differed only in the dict key and variable name).

This is a pure refactor: read_status() and read_current_commit() must
behave identically before and after (verified by the pre-existing
test_metadata_reader.py and test_metadata_reader_status_1623.py suites
staying green). These tests exercise the new shared helper directly to
prove it carries the IDENTICAL error-handling contract the two functions
it replaces had: malformed JSON, non-dict JSON, missing key, non-string
value, and empty string all return None without raising; a valid
non-empty string value is returned as-is.
"""

import json
from pathlib import Path

import pytest

from code_indexer.server.services.metadata_reader import _read_key_from_file


@pytest.fixture
def metadata_path(tmp_path: Path) -> Path:
    return tmp_path / "metadata-voyage-ai.json"


class TestReadKeyFromFileHappyPath:
    def test_returns_string_value_for_present_key(self, metadata_path: Path) -> None:
        metadata_path.write_text(json.dumps({"status": "completed"}))

        assert _read_key_from_file(metadata_path, "status") == "completed"

    def test_reads_a_different_key_from_the_same_file(
        self, metadata_path: Path
    ) -> None:
        metadata_path.write_text(
            json.dumps({"current_commit": "abc123sha", "status": "completed"})
        )

        assert _read_key_from_file(metadata_path, "current_commit") == "abc123sha"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.json"

        assert _read_key_from_file(missing, "status") is None


class TestReadKeyFromFileErrorHandling:
    def test_malformed_json_returns_none(self, metadata_path: Path) -> None:
        metadata_path.write_text("NOT VALID JSON")

        assert _read_key_from_file(metadata_path, "status") is None

    def test_non_dict_json_returns_none(self, metadata_path: Path) -> None:
        metadata_path.write_text(json.dumps(["a", "list", "not", "a", "dict"]))

        assert _read_key_from_file(metadata_path, "status") is None

    def test_missing_key_returns_none(self, metadata_path: Path) -> None:
        metadata_path.write_text(json.dumps({"other_field": "value"}))

        assert _read_key_from_file(metadata_path, "status") is None


class TestReadKeyFromFileValueTypeGuards:
    def test_non_string_value_returns_none(self, metadata_path: Path) -> None:
        metadata_path.write_text(json.dumps({"status": 42}))

        assert _read_key_from_file(metadata_path, "status") is None

    def test_empty_string_value_returns_none(self, metadata_path: Path) -> None:
        metadata_path.write_text(json.dumps({"status": ""}))

        assert _read_key_from_file(metadata_path, "status") is None

    def test_null_value_returns_none(self, metadata_path: Path) -> None:
        metadata_path.write_text(json.dumps({"status": None}))

        assert _read_key_from_file(metadata_path, "status") is None
