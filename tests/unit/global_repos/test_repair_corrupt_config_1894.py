"""
Unit tests proving Bug #1894 RC2: the local-repo repair self-heal
(RefreshScheduler._repair_uninitialized_local_repo, which shells out to a
REAL `cidx init --no-override-file --force` subprocess) must recover a
repo whose .code-indexer/config.json is 0 bytes, contains a bare `""`, or
contains a truncated `{` -- not merely fail with a propagated
`Expecting value: line 1 column 1 (char 0)` decode error.

This is a recurrence of the closed Bug #1769: the earlier fix added a
circuit breaker around repeated repair FAILURES, but never made a single
repair attempt actually succeed against a corrupt (as opposed to merely
absent) config.json. Confirmed live on staging: 260 failed
global_repo_refresh jobs over 3 days for one repo, because every `cidx
init --force` repair attempt kept re-raising the same decode error.

No mocking of `subprocess.run` here -- the whole point is to exercise the
REAL `cidx init` entry point (this environment's editable install resolves
`cidx` to this checkout's src/code_indexer), proving the actual CLI
config-loading code path recovers, not just a scheduler-level retry
counter around a canned failure.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend
from unittest.mock import Mock

ALIAS = "corrupt_config_repo-global"


@pytest.fixture
def golden_repos_dir(tmp_path):
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def golden_repo_metadata_backend():
    with tempfile.TemporaryDirectory() as temp_dir:
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


def _make_corrupt_repo(tmp_path: Path, name: str, corrupt_content: str) -> Path:
    repo_dir = tmp_path / name
    cidx_dir = repo_dir / ".code-indexer"
    cidx_dir.mkdir(parents=True)
    (repo_dir / "hello.py").write_text("print('hello')\n")
    # Simulate already-indexed content that a repair must NOT delete.
    index_dir = cidx_dir / "index"
    index_dir.mkdir()
    (index_dir / "sentinel.bin").write_bytes(b"already-indexed-data")
    (cidx_dir / "config.json").write_text(corrupt_content)
    return repo_dir


@pytest.mark.parametrize(
    "corrupt_content",
    ["", '""', "{"],
    ids=["zero_byte", "bare_json_string", "truncated_brace"],
)
def test_repair_recovers_corrupt_config_via_real_cidx_init(
    scheduler, tmp_path, corrupt_content
):
    repo_dir = _make_corrupt_repo(
        tmp_path, f"repo_{len(corrupt_content)}", corrupt_content
    )

    success, error_detail = scheduler._repair_uninitialized_local_repo(
        str(repo_dir), ALIAS
    )

    assert success is True, (
        f"Repair must recover a corrupt config.json ({corrupt_content!r}) "
        f"without operator action; got error_detail={error_detail!r}"
    )

    config_path = repo_dir / ".code-indexer" / "config.json"
    regenerated = json.loads(config_path.read_text())
    assert regenerated, "Regenerated config.json must contain valid, non-empty JSON"

    # Bug #1894 acceptance: already-indexed data must survive the repair.
    assert (repo_dir / ".code-indexer" / "index" / "sentinel.bin").read_bytes() == (
        b"already-indexed-data"
    )
