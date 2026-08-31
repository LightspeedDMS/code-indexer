"""Unit tests for IDIndexManager.scan_vectors_for_id_map() (Story #1458 AC3
step 1, round-7 Codex-confirmed id_index.bin recreation defect fix).

Fleet migration must obtain the trustworthy point_id -> json_path map via a
SIDE-EFFECT-FREE scan -- NOT the full rebuild_from_vectors(), which
atomically WRITES id_index.bin back to disk as a side effect
(id_index_manager.py:357) BEFORE returning the map. Calling the full
rebuild here would silently RECREATE the exact id_index.bin file Story
#1456 (AC1/AC7) requires be RETIRED for a consolidated collection.

scan_vectors_for_id_map() is the extracted, pure scan-only primitive that
rebuild_from_vectors() itself now delegates to (so both code paths share
ONE scan implementation, never two divergent copies).
"""

import json
import logging
import stat
from pathlib import Path
from typing import Callable, List, Tuple

import pytest

from code_indexer.storage.id_index_manager import IDIndexManager


@pytest.fixture
def make_unreadable(request) -> Callable[[Path], None]:
    """Chmod a given path to 0o000 (genuinely unreadable, even to its own
    owner, since this process runs as a non-root uid) and restore its
    ORIGINAL mode on teardown -- never a hardcoded value, so this fixture
    never depends on assuming any particular starting mode.
    """
    originals: List[Tuple[Path, int]] = []

    def _apply(path: Path) -> None:
        original_mode = stat.S_IMODE(path.stat().st_mode)
        originals.append((path, original_mode))
        path.chmod(0o000)

    def _restore() -> None:
        for path, mode in originals:
            path.chmod(mode)

    request.addfinalizer(_restore)
    return _apply


class TestScanVectorsForIdMapSideEffectFree:
    def test_returns_point_id_to_path_map(self, tmp_path: Path) -> None:
        manager = IDIndexManager()
        v1 = tmp_path / "vector_aaa.json"
        v2 = tmp_path / "vector_bbb.json"
        v1.write_text(json.dumps({"id": "point-1", "vector": [0.1, 0.2]}))
        v2.write_text(json.dumps({"id": "point-2", "vector": [0.3, 0.4]}))

        result = manager.scan_vectors_for_id_map(tmp_path)

        assert result == {"point-1": v1, "point-2": v2}

    def test_never_writes_id_index_bin(self, tmp_path: Path) -> None:
        # The defining behavior under test: this is what distinguishes the
        # scan primitive from rebuild_from_vectors().
        manager = IDIndexManager()
        (tmp_path / "vector_aaa.json").write_text(
            json.dumps({"id": "point-1", "vector": [0.1]})
        )

        manager.scan_vectors_for_id_map(tmp_path)

        assert not (tmp_path / IDIndexManager.INDEX_FILENAME).exists()

    def test_never_writes_id_index_bin_even_when_one_already_exists(
        self, tmp_path: Path
    ) -> None:
        # A pre-existing (possibly stale) id_index.bin must be left
        # completely untouched -- the scan never reads OR rewrites it.
        manager = IDIndexManager()
        (tmp_path / "vector_aaa.json").write_text(
            json.dumps({"id": "point-1", "vector": [0.1]})
        )
        index_file = tmp_path / IDIndexManager.INDEX_FILENAME
        index_file.write_bytes(b"stale-bytes-untouched")

        manager.scan_vectors_for_id_map(tmp_path)

        assert index_file.read_bytes() == b"stale-bytes-untouched"

    def test_empty_collection_returns_empty_map(self, tmp_path: Path) -> None:
        manager = IDIndexManager()
        result = manager.scan_vectors_for_id_map(tmp_path)
        assert result == {}

    def test_skips_collection_meta_json(self, tmp_path: Path) -> None:
        manager = IDIndexManager()
        (tmp_path / "collection_meta.json").write_text(json.dumps({"name": "x"}))
        (tmp_path / "vector_aaa.json").write_text(
            json.dumps({"id": "point-1", "vector": [0.1]})
        )

        result = manager.scan_vectors_for_id_map(tmp_path)

        assert result == {"point-1": tmp_path / "vector_aaa.json"}

    def test_skips_id_index_bin_filename(self, tmp_path: Path) -> None:
        manager = IDIndexManager()
        # id_index.bin isn't valid JSON anyway, but the scan skips it by
        # NAME before ever trying to parse it (mirrors rebuild_from_vectors).
        (tmp_path / IDIndexManager.INDEX_FILENAME).write_bytes(b"binary-not-json")
        (tmp_path / "vector_aaa.json").write_text(
            json.dumps({"id": "point-1", "vector": [0.1]})
        )

        result = manager.scan_vectors_for_id_map(tmp_path)

        assert result == {"point-1": tmp_path / "vector_aaa.json"}

    def test_skips_temporal_bookkeeping_files_without_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        manager = IDIndexManager()
        (tmp_path / "temporal_structure.json").write_text(json.dumps({"marker": 1}))
        (tmp_path / "temporal_progress.json").write_text(json.dumps({"p": 1}))
        (tmp_path / "temporal_meta.json").write_text(json.dumps({"m": 1}))
        (tmp_path / "vector_aaa.json").write_text(
            json.dumps({"id": "point-1", "vector": [0.1]})
        )

        with caplog.at_level(logging.WARNING):
            result = manager.scan_vectors_for_id_map(tmp_path)

        assert result == {"point-1": tmp_path / "vector_aaa.json"}
        assert "temporal_structure.json" not in caplog.text
        assert "temporal_progress.json" not in caplog.text
        assert "temporal_meta.json" not in caplog.text

    def test_skips_chunks_db_content_manifest_filename(
        self, tmp_path: Path, caplog
    ) -> None:
        """Codex CRITICAL finding (round 4): the chunks.db content-
        integrity manifest (collection_migration.py's crash-durable
        content-digest bookkeeping) is migration-engine bookkeeping, not a
        vector record -- must never be scanned/rejected as malformed."""
        manager = IDIndexManager()
        (tmp_path / "chunks_db_content_manifest.json").write_text(
            json.dumps({"point-1": "deadbeef"})
        )
        (tmp_path / "vector_aaa.json").write_text(
            json.dumps({"id": "point-1", "vector": [0.1]})
        )

        with caplog.at_level(logging.WARNING):
            result = manager.scan_vectors_for_id_map(tmp_path)

        assert result == {"point-1": tmp_path / "vector_aaa.json"}
        assert "chunks_db_content_manifest.json" not in caplog.text

    def test_skips_and_warns_on_malformed_json(self, tmp_path: Path, caplog) -> None:
        manager = IDIndexManager()
        (tmp_path / "vector_bad.json").write_text("{not valid json::")
        (tmp_path / "vector_ok.json").write_text(
            json.dumps({"id": "point-1", "vector": [0.1]})
        )

        with caplog.at_level(logging.WARNING):
            result = manager.scan_vectors_for_id_map(tmp_path)

        assert result == {"point-1": tmp_path / "vector_ok.json"}
        assert "vector_bad.json" in caplog.text

    def test_skips_and_warns_on_non_dict_json(self, tmp_path: Path, caplog) -> None:
        manager = IDIndexManager()
        (tmp_path / "vector_list.json").write_text(json.dumps([1, 2, 3]))

        with caplog.at_level(logging.WARNING):
            result = manager.scan_vectors_for_id_map(tmp_path)

        assert result == {}
        assert "vector_list.json" in caplog.text

    def test_skips_and_warns_on_missing_id_field(self, tmp_path: Path, caplog) -> None:
        manager = IDIndexManager()
        (tmp_path / "vector_noid.json").write_text(json.dumps({"vector": [0.1]}))

        with caplog.at_level(logging.WARNING):
            result = manager.scan_vectors_for_id_map(tmp_path)

        assert result == {}
        assert "vector_noid.json" in caplog.text

    def test_skips_and_warns_on_invalid_id_type(self, tmp_path: Path, caplog) -> None:
        manager = IDIndexManager()
        (tmp_path / "vector_badid.json").write_text(
            json.dumps({"id": 12345, "vector": [0.1]})
        )

        with caplog.at_level(logging.WARNING):
            result = manager.scan_vectors_for_id_map(tmp_path)

        assert result == {}
        assert "vector_badid.json" in caplog.text

    def test_scans_nested_hash_shard_subdirectories(self, tmp_path: Path) -> None:
        nested = tmp_path / "aa" / "bb" / "cc" / "dd"
        nested.mkdir(parents=True)
        vfile = nested / "vector_nested.json"
        vfile.write_text(json.dumps({"id": "point-nested", "vector": [0.5]}))

        manager = IDIndexManager()
        result = manager.scan_vectors_for_id_map(tmp_path)

        assert result == {"point-nested": vfile}


class TestScanVectorsForIdMapVerboseRejectionCount:
    """Codex Finding #4 (CRITICAL, Messi Rule #13 anti-silent-failure): a
    genuinely-empty source directory and a directory where every record was
    silently rejected as malformed look IDENTICAL via the bare id_map
    return -- both produce {}. scan_vectors_for_id_map_verbose() surfaces a
    distinct rejected_count so callers (fleet migration) can fail loudly
    instead of treating "all rejected" as "genuinely empty" and flipping
    the discriminator anyway."""

    def test_genuinely_empty_directory_has_zero_rejected_count(
        self, tmp_path: Path
    ) -> None:
        manager = IDIndexManager()

        id_map, rejected_count = manager.scan_vectors_for_id_map_verbose(tmp_path)

        assert id_map == {}
        assert rejected_count == 0

    def test_malformed_json_is_counted_as_rejected(self, tmp_path: Path) -> None:
        manager = IDIndexManager()
        (tmp_path / "vector_bad.json").write_text("{not valid json::")
        (tmp_path / "vector_ok.json").write_text(
            json.dumps({"id": "point-1", "vector": [0.1]})
        )

        id_map, rejected_count = manager.scan_vectors_for_id_map_verbose(tmp_path)

        assert id_map == {"point-1": tmp_path / "vector_ok.json"}
        assert rejected_count == 1

    def test_all_records_rejected_has_nonzero_rejected_count_not_zero(
        self, tmp_path: Path
    ) -> None:
        """The exact hazard this fix closes: a directory where EVERY record
        was rejected must be distinguishable from a genuinely-empty one --
        both currently produce an empty id_map, but rejected_count must
        differ (0 vs >0)."""
        manager = IDIndexManager()
        (tmp_path / "vector_bad1.json").write_text("{not valid json::")
        (tmp_path / "vector_bad2.json").write_text(json.dumps({"vector": [0.1]}))

        id_map, rejected_count = manager.scan_vectors_for_id_map_verbose(tmp_path)

        assert id_map == {}
        assert rejected_count == 2


class TestRebuildFromVectorsDelegatesToScan:
    """rebuild_from_vectors() must still write id_index.bin (unchanged
    external behavior for its existing callers) by delegating to the SAME
    scan_vectors_for_id_map() primitive -- one scan implementation, not two
    divergent copies."""

    def test_rebuild_still_writes_id_index_bin(self, tmp_path: Path) -> None:
        manager = IDIndexManager()
        (tmp_path / "vector_aaa.json").write_text(
            json.dumps({"id": "point-1", "vector": [0.1]})
        )

        result = manager.rebuild_from_vectors(tmp_path)

        assert result == {"point-1": tmp_path / "vector_aaa.json"}
        assert (tmp_path / IDIndexManager.INDEX_FILENAME).exists()

    def test_rebuild_result_matches_pure_scan_result(self, tmp_path: Path) -> None:
        manager = IDIndexManager()
        (tmp_path / "vector_aaa.json").write_text(
            json.dumps({"id": "point-1", "vector": [0.1]})
        )
        (tmp_path / "vector_bbb.json").write_text(
            json.dumps({"id": "point-2", "vector": [0.2]})
        )

        scan_result = manager.scan_vectors_for_id_map(tmp_path)
        rebuild_result = manager.rebuild_from_vectors(tmp_path)

        assert scan_result == rebuild_result


class TestScanVectorsForIdMapDuplicateIdFailsLoud:
    """Codex round-6 CRITICAL finding #5: two distinct shard files sharing
    the same point_id used to be silently collapsed (line 370's
    ``id_index[point_id] = json_file`` overwrites the earlier entry on
    collision) -- only ONE file survived the scan, but cleanup then
    deleted BOTH source files, permanently losing whichever record lost
    the silent race. This is a genuine ambiguity a primary-key store
    cannot resolve automatically -- it must fail loud, naming both
    conflicting paths, and require operator intervention."""

    def test_duplicate_point_id_across_two_files_raises_with_both_paths_named(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.storage.id_index_manager import DuplicateSourceIdError

        manager = IDIndexManager()
        p1 = tmp_path / "aa" / "vector_first.json"
        p2 = tmp_path / "bb" / "vector_second.json"
        p1.parent.mkdir(parents=True)
        p2.parent.mkdir(parents=True)
        p1.write_text(json.dumps({"id": "dup-id", "vector": [1.0]}))
        p2.write_text(json.dumps({"id": "dup-id", "vector": [2.0]}))

        try:
            manager.scan_vectors_for_id_map(tmp_path)
            raised = None
        except DuplicateSourceIdError as exc:
            raised = exc

        assert raised is not None, (
            "Bug: duplicate point_id across two distinct source files was "
            "silently collapsed instead of raising DuplicateSourceIdError."
        )
        assert "dup-id" in str(raised)
        assert str(p1) in str(raised)
        assert str(p2) in str(raised)

    def test_duplicate_point_id_also_raises_via_verbose_variant(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.storage.id_index_manager import DuplicateSourceIdError

        manager = IDIndexManager()
        (tmp_path / "vector_a.json").write_text(
            json.dumps({"id": "dup-id", "vector": [1.0]})
        )
        (tmp_path / "vector_b.json").write_text(
            json.dumps({"id": "dup-id", "vector": [2.0]})
        )

        try:
            manager.scan_vectors_for_id_map_verbose(tmp_path)
            raised = None
        except DuplicateSourceIdError as exc:
            raised = exc

        assert raised is not None

    def test_duplicate_point_id_also_raises_via_rebuild_from_vectors(
        self, tmp_path: Path
    ) -> None:
        """rebuild_from_vectors() delegates to the same scan primitive --
        the fail-loud guarantee must not be bypassable via that entry
        point either."""
        from code_indexer.storage.id_index_manager import DuplicateSourceIdError

        manager = IDIndexManager()
        (tmp_path / "vector_a.json").write_text(
            json.dumps({"id": "dup-id", "vector": [1.0]})
        )
        (tmp_path / "vector_b.json").write_text(
            json.dumps({"id": "dup-id", "vector": [2.0]})
        )

        try:
            manager.rebuild_from_vectors(tmp_path)
            raised = None
        except DuplicateSourceIdError as exc:
            raised = exc

        assert raised is not None
        # And no id_index.bin was written as a side effect of a failed scan.
        assert not (tmp_path / IDIndexManager.INDEX_FILENAME).exists()


class TestScanVectorsForIdMapNonDuplicateIdsUnaffected:
    def test_non_duplicate_ids_are_unaffected(self, tmp_path: Path) -> None:
        manager = IDIndexManager()
        v1 = tmp_path / "vector_a.json"
        v2 = tmp_path / "vector_b.json"
        v1.write_text(json.dumps({"id": "point-1", "vector": [0.1]}))
        v2.write_text(json.dumps({"id": "point-2", "vector": [0.2]}))

        result = manager.scan_vectors_for_id_map(tmp_path)

        assert result == {"point-1": v1, "point-2": v2}


class TestScanVectorsForIdMapUnreadableFile:
    """Bug #1583 dual-review follow-up (opus LOW): a single unreadable file
    (PermissionError/OSError from open(), e.g. a foreign-owned file under a
    collection -- realistic given this project's documented dual-OS-user
    server/auto-updater deployment, Bug #879) must not abort the ENTIRE
    scan. It must be treated the same way a malformed record already is:
    logged, counted as a rejection, and scanning continues over the REST of
    the collection.

    Uses REAL chmod(0o000) via the make_unreadable fixture -- genuine
    OS-level permission denial, not a simulated/mocked exception (this
    process runs as a non-root uid, so chmod 000 genuinely blocks even the
    owner's own open()).
    """

    def test_unreadable_file_is_skipped_others_still_found(
        self, tmp_path: Path, make_unreadable
    ) -> None:
        manager = IDIndexManager()
        ok1 = tmp_path / "vector_ok1.json"
        ok2 = tmp_path / "vector_ok2.json"
        unreadable = tmp_path / "vector_unreadable.json"
        ok1.write_text(json.dumps({"id": "point-1", "vector": [0.1]}))
        ok2.write_text(json.dumps({"id": "point-2", "vector": [0.2]}))
        unreadable.write_text(json.dumps({"id": "point-3", "vector": [0.3]}))
        make_unreadable(unreadable)

        result = manager.scan_vectors_for_id_map(tmp_path)

        assert result == {"point-1": ok1, "point-2": ok2}, (
            "an unreadable file must not abort the whole scan -- the OTHER "
            "genuinely-valid points must still be found"
        )

    def test_unreadable_file_is_counted_as_rejected_via_verbose(
        self, tmp_path: Path, caplog, make_unreadable
    ) -> None:
        manager = IDIndexManager()
        ok1 = tmp_path / "vector_ok1.json"
        unreadable = tmp_path / "vector_unreadable.json"
        ok1.write_text(json.dumps({"id": "point-1", "vector": [0.1]}))
        unreadable.write_text(json.dumps({"id": "point-2", "vector": [0.2]}))
        make_unreadable(unreadable)

        with caplog.at_level(logging.WARNING):
            id_map, rejected_count = manager.scan_vectors_for_id_map_verbose(tmp_path)

        assert id_map == {"point-1": ok1}
        assert rejected_count == 1
        assert "vector_unreadable.json" in caplog.text
