"""Bug #1486 Defect D: quarantined_repos and unrecoverable_repos must be
MUTUALLY EXCLUSIVE dashboard categories.

``record_unrecoverable_corruption()`` writes to the SAME
``fleet_migration_quarantine_state`` table/counter that ordinary
consecutive-failure bookkeeping uses (via ``record_migration_failure``),
just with a distinct ``failure_cause`` of ``UNRECOVERABLE_FAILURE_CAUSE``.
So a repo that first failed a few times (reaching the quarantine
threshold) and then hit an unrecoverable corruption ends up with BOTH a
high consecutive-failure count AND the unrecoverable cause -- and the
pre-fix ``count_quarantined()`` (which filtered ONLY by count) double-
counted it as both quarantined AND unrecoverable, producing an
inconsistent dashboard (e.g. pending=0, quarantined=1, unrecoverable=1 for
a single repo).

``count_quarantined()`` must exclude rows whose ``failure_cause`` is
``UNRECOVERABLE_FAILURE_CAUSE``, keeping the fail-open (return 0 on backend
errors) contract. Real ``GoldenRepoMetadataSqliteBackend`` persistence --
no mocking of the counting logic.
"""

from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.server.services.fleet_migration.discovery import (
    enumerate_fleet_migration_candidates,
)
from code_indexer.server.services.fleet_migration.quarantine import (
    FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
    GENERIC_FAILURE_CAUSE,
    UNRECOVERABLE_FAILURE_CAUSE,
    count_quarantined,
    count_unrecoverable,
    record_migration_failure,
    record_unrecoverable_corruption,
)

from tests.unit.server.services.fleet_migration.test_scheduler_1458 import (
    _FakeGoldenRepoManager,
    _RecordingConfigService,
    _build_already_migrated_repo,
    _make_refresh_scheduler,
    _make_scheduler,
)
from tests.unit.server.services.fleet_migration.test_scheduler_unrecoverable_1486 import (  # noqa: E501
    _build_unrecoverable_corrupt_repo,
    _make_backend,
)


def _seed_failures_then_unrecoverable(golden, alias: str) -> None:
    """Two ordinary generic failures (pushing the count to/over the
    quarantine threshold) followed by an unrecoverable-corruption record --
    reproducing a repo that quarantined FIRST and only later revealed
    permanent, unrecoverable corruption (its legacy source deleted by an
    earlier retry). The last write wins on ``failure_cause``, so the row
    ends up count>=threshold AND cause=UNRECOVERABLE."""
    for i in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD - 1):
        record_migration_failure(
            golden, alias, f"sig{i}", failure_cause=GENERIC_FAILURE_CAUSE
        )
    record_unrecoverable_corruption(golden, alias, "corrupt, legacy already gone")


class TestDefectDQuarantineUnrecoverableMutualExclusivity:
    """Defect D: an unrecoverable repo must NEVER be double-counted as both
    quarantined and unrecoverable."""

    def test_count_quarantined_excludes_unrecoverable_rows(
        self, tmp_path: Path
    ) -> None:
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager({}, sqlite_backend=backend)
        alias = "evolution"

        _seed_failures_then_unrecoverable(golden, alias)

        state = backend.get_fleet_migration_failure_state(alias)
        assert (
            int(state["consecutive_failure_count"])
            >= FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD
        )
        assert state["failure_cause"] == UNRECOVERABLE_FAILURE_CAUSE

        assert count_unrecoverable(golden, [alias]) == 1
        assert count_quarantined(golden, [alias]) == 0, (
            "Defect D: an unrecoverable repo (failure_cause="
            "UNRECOVERABLE_FAILURE_CAUSE) was ALSO counted as quarantined -- "
            "the two dashboard categories must be mutually exclusive."
        )

    def test_get_stats_reports_unrecoverable_repo_only_once(
        self, tmp_path: Path
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        migrated_base = _build_already_migrated_repo(golden_repos_dir, "repo-a-done")
        corrupt_base = _build_unrecoverable_corrupt_repo(
            golden_repos_dir, "repo-b-unrecoverable"
        )
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"repo-a-done": migrated_base, "repo-b-unrecoverable": corrupt_base},
            sqlite_backend=backend,
        )
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        # Resolve the corrupt repo's golden_alias exactly as the scheduler
        # sees it, then seed it into the double-count-prone state.
        candidates = list(enumerate_fleet_migration_candidates(golden))
        corrupt_alias = next(
            c.golden_alias
            for c in candidates
            if Path(c.base_clone_path) == corrupt_base
        )
        _seed_failures_then_unrecoverable(golden, corrupt_alias)

        stats = scheduler.get_stats()

        assert stats["total_repos"] == 2
        assert stats["migrated_repos"] == 1
        assert stats["unrecoverable_repos"] == 1
        assert stats["quarantined_repos"] == 0, (
            "Defect D: the unrecoverable repo was double-counted as "
            "quarantined too -- an inconsistent dashboard (pending=0, "
            "quarantined=1, unrecoverable=1 for a single repo)."
        )
        assert stats["pending_repos"] == 0
