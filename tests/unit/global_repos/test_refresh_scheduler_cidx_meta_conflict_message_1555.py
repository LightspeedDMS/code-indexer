"""Unit test for Bug #1555 Defect A: the Bug #1539 quarantine log message
made a false promise ("Resolves automatically once new commits land
upstream, or requires manual operator intervention.") for a genuine
content-conflict failure. `ConflictResolutionFailedError` (see
server/services/cidx_meta_backup/sync.py) is raised ONLY after the
automatic Claude conflict resolver has already attempted and failed (or
after a successful resolution's `git rebase --continue` itself failed) --
so by the time `_cidx_meta_conflict_quarantine_skip_result` logs this
message, "resolves automatically" is never a live possibility for the
recorded cause. This test asserts the log line is honest: it must not
offer the automatic path, and must plainly state operator intervention is
required.

Mocking scope mirrors the pre-existing
test_refresh_scheduler_cidx_meta_conflict_quarantine_1539.py convention
exactly (only the golden_repo_metadata backend is real).
"""

import logging

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import (
    _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD,
    RefreshScheduler,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


def _make_scheduler_with_real_backend(tmp_path):
    golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
    golden_repos_dir.mkdir(parents=True)
    (golden_repos_dir / "cidx-meta").mkdir()

    class _RegistryStub:
        def get_global_repo(self, alias_name):
            return {"repo_url": "local://cidx-meta", "default_branch": "master"}

        def update_refresh_timestamp(self, alias_name):
            return None

    backend = GoldenRepoMetadataSqliteBackend(str(tmp_path / "metadata.db"))
    backend.ensure_table_exists()

    sched = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=ConfigManager(tmp_path / ".code-indexer" / "config.json"),
        query_tracker=QueryTracker(),
        cleanup_manager=CleanupManager(QueryTracker()),
        registry=_RegistryStub(),
        golden_repo_metadata_backend=backend,
    )
    return sched, backend


def test_quarantine_message_does_not_promise_automatic_resolution(tmp_path, caplog):
    sched, backend = _make_scheduler_with_real_backend(tmp_path)
    try:
        for _ in range(_CIDX_META_CONFLICT_QUARANTINE_THRESHOLD):
            backend.record_cidx_meta_conflict_failure(
                "cidx-meta-global", "b9aff48", "rebase failing at commit 2 of 27"
            )

        with caplog.at_level(
            logging.ERROR, logger="code_indexer.global_repos.refresh_scheduler"
        ):
            skip_result = sched._cidx_meta_conflict_quarantine_skip_result(
                "cidx-meta-global", "b9aff48"
            )

        assert skip_result is not None
        quarantine_messages = [
            record.message
            for record in caplog.records
            if "QUARANTINED" in record.message
        ]
        assert len(quarantine_messages) == 1
        message = quarantine_messages[0]

        # The false promise this bug reports: must not be present.
        assert "resolves automatically" not in message.lower()

        # The honest replacement: must plainly require operator intervention.
        assert "manual operator intervention is required" in message.lower()
    finally:
        backend.close()
