"""
Story #908 AC7: Path resolution honors CIDX_DATA_DIR env var (Bug #879 IPC alignment).

Module-level symbols:
- _make_journal_default(): private helper — avoids repeating RepairJournal import in each test
- test_cidx_data_dir_env_override_used
- test_cidx_data_dir_unset_defaults_to_home_cidx_server
- test_parent_directory_created_if_missing: asserts deep parent dir is created (real behavior
  proof of parents=True, exist_ok=True — no mocking per MESSI Rule 1)
"""

import os
from unittest.mock import patch

_JOURNAL_FILENAME = "dep_map_repair_journal.jsonl"
_DEFAULT_SUFFIX = f".cidx-server/{_JOURNAL_FILENAME}"


def _make_journal_default():
    """Instantiate RepairJournal with no path argument (uses env var / default)."""
    from code_indexer.server.services.dep_map_repair_executor import RepairJournal

    return RepairJournal()


def test_cidx_data_dir_env_override_used(tmp_path):
    """When CIDX_DATA_DIR is set, journal path resolves under that directory."""
    custom_dir = tmp_path / "custom-data-dir"
    with patch.dict(os.environ, {"CIDX_DATA_DIR": str(custom_dir)}):
        journal = _make_journal_default()

    assert str(custom_dir) in str(journal.journal_path), (
        f"Journal path {journal.journal_path} does not use CIDX_DATA_DIR={custom_dir}"
    )
    assert journal.journal_path.name == _JOURNAL_FILENAME


def test_cidx_data_dir_unset_defaults_to_home_cidx_server():
    """When CIDX_DATA_DIR is unset, journal defaults to ~/.cidx-server/dep_map_repair_journal.jsonl."""
    env_without = {k: v for k, v in os.environ.items() if k != "CIDX_DATA_DIR"}
    with patch.dict(os.environ, env_without, clear=True):
        journal = _make_journal_default()

    assert str(journal.journal_path).endswith(_DEFAULT_SUFFIX), (
        f"Expected journal ending with {_DEFAULT_SUFFIX!r}. Got: {journal.journal_path}"
    )


def test_parent_directory_created_if_missing(tmp_path):
    """RepairJournal creates missing parent directories at construction.

    Verifies by behavioral observation: a deeply nested directory that did not
    exist before construction exists after. This is the real behavioral proof
    of parents=True, exist_ok=True without mocking the call itself (MESSI Rule 1).
    """
    deep_dir = tmp_path / "deep" / "nested" / "dir"
    assert not deep_dir.exists(), "Pre-condition: directory must not exist before test"

    with patch.dict(os.environ, {"CIDX_DATA_DIR": str(deep_dir)}):
        _make_journal_default()

    assert deep_dir.exists(), (
        f"RepairJournal must create missing parent directory: {deep_dir}"
    )
