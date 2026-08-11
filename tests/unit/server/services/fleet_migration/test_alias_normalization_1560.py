"""
Codex review Finding F7: golden-alias normalization is documented (the
"-global" suffix convention) but not implemented at the quarantine.py /
dedup_state.py read+write boundaries -- "foo" and "foo-global" become
TWO separate primary-key rows, so /health can double-report (or
disagree about) one logical repo.

Fix: ONE centralized `normalize_golden_alias()` helper (mirroring the
already-established Bug #1373 bare/`-global` normalization pattern),
applied at every quarantine.py/dedup_state.py function that reads or
writes a `golden_alias`-keyed row.

Real SQLite backend, no mocking of the module under test -- mirrors
test_dedup_quarantine_reset_1560.py's and
test_dedup_state_1560.py's own fixture conventions.
"""

import os
import tempfile

import pytest

from code_indexer.server.services.fleet_migration.alias_normalization import (
    GLOBAL_SUFFIX,
    normalize_golden_alias,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


class _FakeGoldenRepoManagerWithBackend:
    def __init__(self, sqlite_backend):
        self._sqlite_backend = sqlite_backend


@pytest.fixture
def backend():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        be = GoldenRepoMetadataSqliteBackend(db_path)
        be.ensure_table_exists()
        try:
            yield be
        finally:
            be.close()


def _record_default_dedup_outcome(manager, alias: str, **overrides) -> None:
    """Shared helper: records one dedup outcome with sensible defaults,
    letting each test override only the fields it cares about."""
    from code_indexer.server.services.fleet_migration.dedup_state import (
        record_dedup_outcome,
    )

    kwargs = {
        "duplicate_groups": 1,
        "records_before": 10,
        "records_deleted": 1,
        "winner_kept_groups": 1,
        "whole_group_deleted_groups": 0,
        "collection_total": 10,
    }
    kwargs.update(overrides)
    record_dedup_outcome(manager, alias, **kwargs)


class TestNormalizeGoldenAliasStripping:
    def test_strips_trailing_global_suffix(self):
        assert normalize_golden_alias("click-global") == "click"

    def test_bare_alias_unchanged(self):
        assert normalize_golden_alias("click") == "click"

    def test_global_suffix_constant_matches_stripped_text(self):
        alias = "evolution" + GLOBAL_SUFFIX
        assert normalize_golden_alias(alias) == "evolution"


class TestNormalizeGoldenAliasExactlyOneSuffix:
    def test_substring_elsewhere_is_not_stripped(self):
        """An alias that legitimately contains "-global" as a substring
        elsewhere (not as the trailing suffix) must be left alone."""
        assert normalize_golden_alias("global-services") == "global-services"

    def test_only_the_trailing_suffix_is_removed_once(self):
        """A double-suffixed alias strips exactly ONE trailing
        occurrence, never both -- proves this is a single trailing-
        suffix strip, not a repeated/greedy one."""
        assert normalize_golden_alias("click-global-global") == "click-global"


class TestQuarantineAliasNormalization:
    """quarantine.py: write via one alias form, read via the other."""

    def test_write_bare_read_global_same_row(self, backend):
        from code_indexer.server.services.fleet_migration.quarantine import (
            get_failure_state,
            record_migration_failure,
        )

        manager = _FakeGoldenRepoManagerWithBackend(backend)
        record_migration_failure(manager, "click", "sig-1")

        state = get_failure_state(manager, "click-global")

        assert state is not None
        assert state["consecutive_failure_count"] == 1

    def test_write_global_read_bare_same_row(self, backend):
        from code_indexer.server.services.fleet_migration.quarantine import (
            get_failure_state,
            record_migration_failure,
        )

        manager = _FakeGoldenRepoManagerWithBackend(backend)
        record_migration_failure(manager, "click-global", "sig-1")

        state = get_failure_state(manager, "click")

        assert state is not None
        assert state["consecutive_failure_count"] == 1

    def test_repeated_failures_across_both_forms_accumulate_on_one_row(self, backend):
        """Two failures recorded via DIFFERENT alias forms for the SAME
        logical repo must accumulate on ONE row, never two."""
        from code_indexer.server.services.fleet_migration.quarantine import (
            get_failure_state,
            record_migration_failure,
        )

        manager = _FakeGoldenRepoManagerWithBackend(backend)
        record_migration_failure(manager, "click", "sig-1")
        record_migration_failure(manager, "click-global", "sig-1")

        state = get_failure_state(manager, "click")
        assert state is not None
        assert state["consecutive_failure_count"] == 2


class TestDedupStateAliasNormalizationReadWrite:
    """dedup_state.py: write via one alias form, read via the other."""

    def test_write_bare_read_global_same_row(self, backend):
        from code_indexer.server.services.fleet_migration.dedup_state import (
            get_dedup_state,
        )

        manager = _FakeGoldenRepoManagerWithBackend(backend)
        _record_default_dedup_outcome(manager, "click")

        state = get_dedup_state(manager, "click-global")

        assert state is not None
        assert state["records_deleted"] == 1

    def test_write_global_read_bare_same_row(self, backend):
        from code_indexer.server.services.fleet_migration.dedup_state import (
            get_dedup_state,
        )

        manager = _FakeGoldenRepoManagerWithBackend(backend)
        _record_default_dedup_outcome(manager, "click-global")

        state = get_dedup_state(manager, "click")

        assert state is not None
        assert state["records_deleted"] == 1


class TestDedupStateAliasNormalizationAggregateSurfaces:
    """list_dedup_states/clear_dedup_state must also treat both alias
    forms as the SAME logical repo."""

    def test_list_dedup_states_reports_one_row_not_two(self, backend):
        """Recording outcomes via BOTH alias forms for the SAME logical
        repo must never leave two separate rows visible on the /health
        listing surface."""
        from code_indexer.server.services.fleet_migration.dedup_state import (
            list_dedup_states,
        )

        manager = _FakeGoldenRepoManagerWithBackend(backend)
        _record_default_dedup_outcome(manager, "click", records_before=10)
        _record_default_dedup_outcome(manager, "click-global", records_before=9)

        states = list_dedup_states(manager)

        assert len(states) == 1
        assert states[0]["golden_alias"] == "click"
        assert states[0]["records_deleted"] == 2

    def test_clear_via_global_clears_row_written_via_bare(self, backend):
        from code_indexer.server.services.fleet_migration.dedup_state import (
            clear_dedup_state,
            get_dedup_state,
        )

        manager = _FakeGoldenRepoManagerWithBackend(backend)
        _record_default_dedup_outcome(manager, "click")

        clear_dedup_state(manager, "click-global", "successful full re-index")

        state = get_dedup_state(manager, "click")
        assert state is not None
        assert state["cleared_at"] is not None
