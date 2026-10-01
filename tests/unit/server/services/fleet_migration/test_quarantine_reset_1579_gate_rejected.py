"""
Bug #1579 Part 4: `reset_duplicate_caused_quarantine_if_resolved` must not
depend on the GATE-FILTERED `collection_has_duplicate_point_ids` predicate,
which returns False for exactly the gate-rejected collections whose
quarantine this reset exists to unblock.

Root cause: a collection can have a genuine duplicate point_id (two
on-disk records sharing the same `id`, both individually
self-consistent) AND, elsewhere in the SAME collection, one unrelated
record whose `unique_key` is missing/foreign -- which makes the
whole-collection identity gate
(`collection_dedup_repair._whole_collection_identity_gate_passes`)
reject the ENTIRE collection. Pre-fix, `collection_has_duplicate_point_ids`
(scoped to "duplicates THIS REPAIR WOULD AUTO-RESOLVE") returns False for
such a collection even though a real duplicate is present, so
`reset_duplicate_caused_quarantine_if_resolved` never fires and the repo
stays quarantined forever -- the exact non-converging quarantine loop
issue #1579 reports for `cidx-meta` and friends.

Fixed by switching the predicate this reset consults to the gate-AGNOSTIC
`collection_has_any_duplicate_point_ids` (Bug #1579, added in
`collection_dedup_repair.py`), which answers "is there a real duplicate
point_id on disk right now, period" without consulting the identity gate
at all.

IMPORTANT second-order fix, found via a real regression while wiring this
up: making the reset UNCONDITIONALLY gate-agnostic reproduces Bug #1477's
fleet-starvation bug for a collection that will NEVER pass the gate (e.g.
legacy records with no `unique_key` at all, permanently) -- every tick
would reset the quarantine, immediately re-attempt, immediately re-fail
with the SAME "dedup_gate_rejected" status, and re-quarantine, hogging
every scheduler tick and starving every alphabetically-later candidate
(confirmed empirically: `test_scheduler_quarantine_warning_cadence_1565.py`
regressed from "nothing_to_migrate" -- correctly skipped -- to
"dedup_gate_rejected" -- re-attempted every tick -- the moment the
gate-agnostic swap landed). The fix is a NEW, distinct, PERSISTED failure
cause: `DEDUP_GATE_REJECTED_FAILURE_CAUSE` (classified by
`classify_failure_cause()` from the orchestrator's own "dedup_gate_rejected"
detail text), excluded from this reset's scope exactly like
`UNRECOVERABLE_FAILURE_CAUSE`/`DISK_HEADROOM_FAILURE_CAUSE` already are --
a repo whose quarantine is KNOWN (via this distinct cause) to be caused by
a gate rejection stays quarantined (no futile retry, no starvation); the
gate-agnostic predicate remains useful ONLY for legacy/GENERIC-cause
quarantines (e.g. from a pre-Bug-1579 crash) to get ONE fair re-evaluation,
after which they are correctly reclassified and never bounce again.

Mirrors `test_dedup_quarantine_reset_1560.py`'s exact fixture conventions
(real SQLite backend, real on-disk collection directories, no mocking of
the module under test) and extends it with a gate-rejecting fixture.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.services.fleet_migration.discovery import (
    FleetMigrationCandidate,
)
from code_indexer.server.services.fleet_migration.quarantine import (
    DEDUP_GATE_REJECTED_FAILURE_CAUSE,
    FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
    GENERIC_FAILURE_CAUSE,
    classify_failure_cause,
    get_failure_state,
    record_migration_failure,
    reset_duplicate_caused_quarantine_if_resolved,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


class _FakeGoldenRepoManagerWithBackend:
    def __init__(self, sqlite_backend):
        self._sqlite_backend = sqlite_backend


def _quarantine_with_generic_cause(backend, alias: str, count: int) -> None:
    for _ in range(count):
        record_migration_failure(
            _FakeGoldenRepoManagerWithBackend(backend),
            alias,
            "sig",
            failure_cause=GENERIC_FAILURE_CAUSE,
        )


def _quarantine_with_dedup_gate_rejected_cause(backend, alias: str, count: int) -> None:
    for _ in range(count):
        record_migration_failure(
            _FakeGoldenRepoManagerWithBackend(backend),
            alias,
            "sig",
            failure_cause=DEDUP_GATE_REJECTED_FAILURE_CAUSE,
        )


def _point_id(unique_key: str) -> str:
    return hashlib.md5(unique_key.encode()).hexdigest()


def _write_self_consistent_duplicate_pair(collection_dir: Path) -> None:
    """Two records sharing the SAME point_id, BOTH individually
    self-consistent (id == md5(unique_key)) -- a genuine, repair-
    resolvable duplicate IN ISOLATION."""
    unique_key = "proj_sha256:gaterejected579_0"
    point_id = _point_id(unique_key)
    for suffix in ("-a", "-b"):
        record = {
            "id": point_id,
            "vector": [0.1, 0.2],
            "payload": {
                "unique_key": unique_key,
                "chunk_index": 0,
                "total_chunks": 1,
                "line_start": 1,
                "line_end": 5,
            },
        }
        shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + suffix)
        shard_dir.mkdir(parents=True, exist_ok=True)
        (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


def _write_hidden_branches_only_sentinel_record(collection_dir: Path) -> None:
    """Issue #1751: the EXACT confirmed production bookkeeping-sentinel
    shape Bug #1747 fixed `_whole_collection_identity_gate_passes` to
    SKIP (never treat as a gate-failure trigger): payload carries ONLY
    `hidden_branches`, no `unique_key`/`path`/`content`, and top-level
    `chunk_text` is the empty string. Present alongside a genuine
    duplicate pair, this must NOT make the whole-collection identity gate
    reject the collection post-#1747 -- `collection_has_duplicate_point_
    ids` (the gate-AWARE predicate) now genuinely returns True for this
    exact combination, which is the scenario issue #1751's quarantine-
    reset fix must be able to see."""
    point_id = "hiddenbranchsentinelfeedcafe0001"
    record = {
        "id": point_id,
        "chunk_text": "",
        "payload": {
            "hidden_branches": ["some-branch"],
        },
    }
    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


def _write_gate_breaking_single_record(collection_dir: Path) -> None:
    """ONE unrelated, non-duplicated record with NO `unique_key` at all --
    this is what makes `_whole_collection_identity_gate_passes` reject
    the WHOLE collection (per collection_dedup_repair.py), even though
    the duplicate pair above is, in isolation, perfectly resolvable."""
    point_id = "gatebreakerfeedfacecafefeed0001"
    record = {
        "id": point_id,
        "vector": [0.3, 0.4],
        "payload": {
            "chunk_index": 0,
            "total_chunks": 1,
            "line_start": 1,
            "line_end": 5,
        },
    }
    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


def _make_candidate(
    tmp_path: Path,
    alias: str = "click",
    *,
    with_duplicate: bool,
    gate_rejected: bool,
    with_hidden_branches_sentinel: bool = False,
) -> FleetMigrationCandidate:
    base_clone = tmp_path / alias
    index_path = base_clone / ".code-indexer" / "index"
    collection_dir = index_path / "semantic_collection"
    collection_dir.mkdir(parents=True)
    (collection_dir / "collection_meta.json").write_text("{}")
    if with_duplicate:
        _write_self_consistent_duplicate_pair(collection_dir)
    if gate_rejected:
        _write_gate_breaking_single_record(collection_dir)
    if with_hidden_branches_sentinel:
        _write_hidden_branches_only_sentinel_record(collection_dir)

    return FleetMigrationCandidate(
        sort_key=alias,
        golden_alias=alias,
        base_clone_path=base_clone,
        index_path=index_path,
        semantic_collection_dirs=[collection_dir],
        temporal_namespaces=[],
        sister_root=tmp_path / "golden-repos",
        sister_alias_manager=AliasManager(str(tmp_path / "golden-repos" / "aliases")),
    )


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


class TestResetFiresForGateRejectedDuplicateQuarantine:
    def test_resets_when_gate_rejected_collection_still_has_raw_duplicate(
        self, tmp_path, backend
    ):
        """Bug #1579: the gate-agnostic predicate must see the duplicate
        even though the whole-collection identity gate rejects this
        collection (a missing-unique_key record sits elsewhere in it).
        This models a LEGACY/GENERIC-cause quarantine (e.g. from a
        pre-Bug-1579 crash) getting its one fair re-evaluation."""
        candidate = _make_candidate(tmp_path, with_duplicate=True, gate_rejected=True)
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        _quarantine_with_generic_cause(
            backend,
            candidate.golden_alias,
            FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
        )

        result = reset_duplicate_caused_quarantine_if_resolved(manager, candidate)

        assert result is True, (
            "a gate-rejected collection quarantined under the GENERIC "
            "cause that STILL has a genuine raw duplicate point_id must "
            "get a fair reset/retry -- the pre-fix gate-FILTERED "
            "predicate (collection_has_duplicate_point_ids) returns "
            "False here, permanently defeating this reset and "
            "reproducing issue #1579's non-converging quarantine loop"
        )
        assert get_failure_state(manager, candidate.golden_alias) is None

    def test_no_reset_when_gate_rejected_collection_has_no_duplicate_at_all(
        self, tmp_path, backend
    ):
        """Regression: a gate-rejected collection with genuinely NO
        duplicate anywhere must still correctly report no reset -- the
        gate-agnostic predicate is broader for DUPLICATES, not a signal
        that fires unconditionally whenever the gate merely rejects."""
        candidate = _make_candidate(tmp_path, with_duplicate=False, gate_rejected=True)
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        _quarantine_with_generic_cause(
            backend,
            candidate.golden_alias,
            FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
        )

        result = reset_duplicate_caused_quarantine_if_resolved(manager, candidate)

        assert result is False
        assert get_failure_state(manager, candidate.golden_alias) is not None


class TestDedupGateRejectedCauseNeverResets:
    """The second-order fix: a quarantine KNOWN (via a distinct,
    persisted failure_cause) to be caused by a gate rejection must NEVER
    be reset by this mechanism, exactly like UNRECOVERABLE_FAILURE_CAUSE/
    DISK_HEADROOM_FAILURE_CAUSE -- otherwise a collection that will never
    pass the gate gets reset, immediately re-attempted, immediately
    re-fails, and re-quarantines on every single tick, starving every
    alphabetically-later fleet-migration candidate (Bug #1477's own
    failure mode, reproduced by the naive gate-agnostic-only fix)."""

    def test_classify_failure_cause_maps_dedup_gate_rejected_detail(self) -> None:
        # Exact detail text orchestrator.py's _run_migration_sequence
        # produces for FleetMigrationRepoResult(status="dedup_gate_rejected").
        detail = (
            "1 collection(s) rejected by the whole-collection identity "
            "gate with duplicate point_id group(s) present -- requires "
            "manual review, will count toward quarantine"
        )

        assert (
            classify_failure_cause(detail=detail) == DEDUP_GATE_REJECTED_FAILURE_CAUSE
        )

    def test_no_reset_when_cause_is_dedup_gate_rejected(
        self, tmp_path, backend
    ) -> None:
        """A collection with a genuine duplicate present, quarantined
        under the NEW distinct dedup_gate_rejected cause, must NOT be
        reset -- unlike the GENERIC-cause case above, this cause is a
        KNOWN, confirmed gate rejection: retrying is futile and would
        only starve other candidates."""
        candidate = _make_candidate(tmp_path, with_duplicate=True, gate_rejected=True)
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        _quarantine_with_dedup_gate_rejected_cause(
            backend,
            candidate.golden_alias,
            FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
        )

        result = reset_duplicate_caused_quarantine_if_resolved(manager, candidate)

        assert result is False, (
            "a repo quarantined under the distinct DEDUP_GATE_REJECTED_"
            "FAILURE_CAUSE must never be reset by this mechanism -- "
            "doing so causes an infinite reset-then-immediately-refail "
            "cycle that hogs every scheduler tick and starves every "
            "alphabetically-later candidate (Bug #1477's starvation "
            "failure mode, reproduced)"
        )
        state = get_failure_state(manager, candidate.golden_alias)
        assert state is not None
        assert state["failure_cause"] == DEDUP_GATE_REJECTED_FAILURE_CAUSE


class TestDedupGateRejectedCauseResetsWhenGateNowGenuinelyPasses:
    """Issue #1751: #1747 fixed the whole-collection identity gate to
    SKIP a hidden_branches-only bookkeeping sentinel record rather than
    treating its missing unique_key as a gate-failure trigger. For the
    SUBSET of dedup_gate_rejected quarantines caused solely by that
    sentinel shape, the gate now genuinely passes -- a bare retry would
    genuinely succeed. But `reset_duplicate_caused_quarantine_if_
    resolved` hard-excludes DEDUP_GATE_REJECTED_FAILURE_CAUSE
    unconditionally (Bug #1579's original, now partially-stale,
    rationale), and the only other escape hatch (`is_quarantined()`'s
    directory-content-signature auto-clear) never fires for a repo with
    no new commits since quarantine. This class proves the gap and its
    fix: a one-shot re-evaluation of the GATE-AWARE
    `collection_has_duplicate_point_ids` predicate (the exact function
    #1747 fixed), independent of any content-signature change."""

    def test_resets_when_gate_now_passes_due_to_1747_sentinel_fix(
        self, tmp_path, backend
    ) -> None:
        candidate = _make_candidate(
            tmp_path,
            with_duplicate=True,
            gate_rejected=False,
            with_hidden_branches_sentinel=True,
        )
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        _quarantine_with_dedup_gate_rejected_cause(
            backend,
            candidate.golden_alias,
            FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
        )

        result = reset_duplicate_caused_quarantine_if_resolved(manager, candidate)

        assert result is True, (
            "post-#1747, the whole-collection identity gate genuinely "
            "passes for a collection whose only gate-adjacent record is "
            "a hidden_branches-only bookkeeping sentinel -- a "
            "dedup_gate_rejected quarantine for exactly this shape must "
            "self-heal via a one-shot gate re-evaluation "
            "(collection_has_duplicate_point_ids), independent of the "
            "directory-content-signature auto-clear path (issue #1751) "
            "-- a repo with no new commits since quarantine must not "
            "stay stuck forever"
        )
        assert get_failure_state(manager, candidate.golden_alias) is None
