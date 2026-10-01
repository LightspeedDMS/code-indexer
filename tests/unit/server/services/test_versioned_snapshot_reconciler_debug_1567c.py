"""Bug #1567c: per-namespace DEBUG decision-reasoning logging for the
Bug #1567 versioned-snapshot orphan sweep.

The sweep deletes unconditionally (there is no "report" vs "delete"
mode -- that config-mode wrapper was removed as an off-by-default
toggle a bug fix must not ship behind). An operator still needs to
inspect WHY each snapshot in a namespace was kept or became a deletion
candidate. This module drives `reconcile_versioned_snapshots(...)` directly (real
filesystem + real AliasManager/VersionedSnapshotManager/CleanupManager,
mirroring the `_make_env`/`_make_snapshot_dir` helpers from
test_versioned_snapshot_reconciler_1567.py) and asserts on the DEBUG-only
per-namespace breakdown a new `_debug_log_namespace_decision` helper must
emit -- gated on `logger.isEnabledFor(logging.DEBUG)` so it costs nothing
at INFO level in production.
"""

from __future__ import annotations

import logging

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.services.versioned_snapshot_reconciler import (
    reconcile_versioned_snapshots,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)

_RECONCILER_LOGGER = "code_indexer.server.services.versioned_snapshot_reconciler"

#: keep_last=1 protects ONLY the live snapshot itself -- zero protection
#: from history (matches test_versioned_snapshot_reconciler_1567.py's
#: KEEP_LAST_MINIMAL convention).
KEEP_LAST_MINIMAL = 1


def _make_env(tmp_path):
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir()
    aliases_dir = golden_repos_dir / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    snapshot_manager = VersionedSnapshotManager(versioned_base=str(golden_repos_dir))
    cleanup_manager = CleanupManager(query_tracker=QueryTracker())
    return golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager


def _make_snapshot_dir(golden_repos_dir, bare_namespace: str, ts: int) -> str:
    path = golden_repos_dir / ".versioned" / bare_namespace / f"v_{ts}"
    path.mkdir(parents=True)
    return str(path)


def _decision_records(caplog):
    return [r for r in caplog.records if "decision --" in r.getMessage()]


class TestNamespaceDecisionDebugLogging:
    def test_leaked_older_snapshot_produces_a_decision_record_with_real_counts(
        self, tmp_path, caplog
    ):
        """One namespace: one leaked (superseded) older snapshot plus the
        live snapshot, keep_last=1 (zero history protection) -> the older
        snapshot becomes a candidate. The DEBUG record must reflect this
        exactly: ts_live matches the live snapshot's own timestamp,
        older=1, at_or_newer_than_live(kept)=1 (the live snapshot itself),
        candidates=1."""
        golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
            tmp_path
        )
        bare_ns = "myrepo"
        TS_LEAKED = 1000
        TS_LIVE = 2000
        _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LEAKED)
        current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
        alias_manager.create_alias(f"{bare_ns}-global", current, repo_name=bare_ns)

        with caplog.at_level(logging.DEBUG, logger=_RECONCILER_LOGGER):
            result = reconcile_versioned_snapshots(
                str(golden_repos_dir),
                snapshot_manager=snapshot_manager,
                alias_manager=alias_manager,
                cleanup_manager=cleanup_manager,
                retention_keep_last=KEEP_LAST_MINIMAL,
            )

        assert result.scheduled_paths  # sanity: candidate really found
        decisions = _decision_records(caplog)
        assert decisions, (
            "no DEBUG decision record found -- expected one per namespace "
            f"reconciled. All records: {[r.getMessage() for r in caplog.records]}"
        )
        message = decisions[0].getMessage()
        assert bare_ns in message
        assert f"ts_live={TS_LIVE}" in message
        assert "older=1" in message
        assert "at_or_newer_than_live(kept)=1" in message
        assert "candidates=1" in message

    def test_keep_last_protection_yields_zero_candidates_and_is_reflected(
        self, tmp_path, caplog
    ):
        """retention_keep_last=2 with exactly ONE older snapshot below the
        live one: keep_from_history = max(keep_last - 1, 0) = 1, so that
        single older snapshot is fully protected by keep-last history
        retention and zero candidates result. (Two older snapshots under
        keep_last=2 would leave one unprotected/candidate -- inconsistent
        with a "candidates=0" expectation -- so this scenario uses one
        older snapshot, which is the only shape that makes
        `within_keep_last_2(kept) >= 1` and `candidates=0` simultaneously
        true.)"""
        golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
            tmp_path
        )
        bare_ns = "myrepo2"
        TS_OLD = 1000
        TS_LIVE = 2000
        _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD)
        current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
        alias_manager.create_alias(f"{bare_ns}-global", current, repo_name=bare_ns)

        with caplog.at_level(logging.DEBUG, logger=_RECONCILER_LOGGER):
            result = reconcile_versioned_snapshots(
                str(golden_repos_dir),
                snapshot_manager=snapshot_manager,
                alias_manager=alias_manager,
                cleanup_manager=cleanup_manager,
                retention_keep_last=2,
            )

        assert result.scheduled_paths == []  # sanity: fully protected
        decisions = _decision_records(caplog)
        assert decisions, (
            "no DEBUG decision record found -- expected one per namespace "
            f"reconciled. All records: {[r.getMessage() for r in caplog.records]}"
        )
        message = decisions[0].getMessage()
        assert bare_ns in message
        assert "within_keep_last_2(kept)=1" in message
        assert "candidates=0" in message

    def test_decision_logging_is_silent_at_info_level(self, tmp_path, caplog):
        """The exact same leaked-snapshot scenario as the first test, but
        with the reconciler's logger only enabled at INFO -- the decision
        breakdown must not be computed/emitted (it is gated on
        logger.isEnabledFor(logging.DEBUG), not merely filtered by the
        handler, so this also proves it never leaks into a production
        INFO-level log stream)."""
        golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
            tmp_path
        )
        bare_ns = "myrepo3"
        TS_LEAKED = 1000
        TS_LIVE = 2000
        _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LEAKED)
        current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
        alias_manager.create_alias(f"{bare_ns}-global", current, repo_name=bare_ns)

        with caplog.at_level(logging.INFO, logger=_RECONCILER_LOGGER):
            reconcile_versioned_snapshots(
                str(golden_repos_dir),
                snapshot_manager=snapshot_manager,
                alias_manager=alias_manager,
                cleanup_manager=cleanup_manager,
                retention_keep_last=KEEP_LAST_MINIMAL,
            )

        assert not _decision_records(caplog), (
            "decision breakdown must not appear when the reconciler's "
            "logger is only enabled at INFO level"
        )
