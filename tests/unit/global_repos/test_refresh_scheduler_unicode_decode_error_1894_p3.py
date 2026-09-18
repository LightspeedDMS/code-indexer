"""
Discriminating test for the P2 review finding on #1894/#1895:
`RefreshScheduler._is_local_config_valid()` catches only
`(json.JSONDecodeError, OSError)`, but NOT `UnicodeDecodeError` -- which is
a `ValueError` subclass, not an `OSError`/`JSONDecodeError` subclass, so it
is NOT caught by that tuple. A partially-corrupt, non-UTF-8 config.json
therefore raises uncaught out of `_is_local_config_valid()`.

The scheduler's Bug #1769/#1253 quarantine gate calls
`_is_local_config_valid()` OUTSIDE its metadata-read try block (see
`_execute_refresh` around line 2274), so this uncaught exception crashes
the whole scheduled refresh cycle for the repo instead of being treated as
"config invalid, attempt repair" like any other corrupt config.

This test fails on the BEHAVIOUR (an uncaught UnicodeDecodeError escapes
_is_local_config_valid()) before the fix, not on a missing symbol.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


@pytest.fixture
def golden_repos_dir(tmp_path):
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def golden_repo_metadata_backend():
    with tempfile.TemporaryDirectory() as temp_dir:
        import os

        db_path = os.path.join(temp_dir, "test.db")
        be = GoldenRepoMetadataSqliteBackend(db_path)
        be.ensure_table_exists()
        yield be


@pytest.fixture
def scheduler(golden_repos_dir, golden_repo_metadata_backend):
    mock_config_source = Mock()
    mock_config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=Mock(),
        golden_repo_metadata_backend=golden_repo_metadata_backend,
    )


def test_is_local_config_valid_treats_non_utf8_config_as_invalid_not_a_crash(
    scheduler, tmp_path: Path
) -> None:
    """A non-UTF-8 config.json must make _is_local_config_valid() return
    False (config is invalid, eligible for repair), not raise an uncaught
    UnicodeDecodeError that crashes the whole scheduled refresh cycle."""
    config_path = tmp_path / "config.json"
    # 0xFF is not valid UTF-8 in this position -- triggers
    # UnicodeDecodeError when read in default text mode.
    config_path.write_bytes(b'{"codebase_dir": "\xff\xfe broken"}')

    result = scheduler._is_local_config_valid(config_path)

    assert result is False, (
        "expected _is_local_config_valid() to gracefully treat a non-UTF-8 "
        "config as invalid (False), not raise UnicodeDecodeError uncaught"
    )
