"""EVO-65484: registry reconcile — backfill golden repos that are indexed on disk
but missing from the registry.

Registration persists the index before the registry row, so an interrupted job
(pod eviction / crash) leaves index-without-entry; the workspace-mcp seeder then
sees the repo as unregistered and re-provisions it (EVO-65481). This reconcile
heals the drift by backfilling a registry row for every indexed-on-disk repo that
has no entry — never re-indexing, idempotent, tolerant of concurrent sibling pods.
"""

import sqlite3
from unittest.mock import MagicMock

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


def _mk_indexed_repo(root, name):
    d = root / name
    idx = d / ".code-indexer" / "index" / "voyage-code-3"
    idx.mkdir(parents=True)
    (idx / "hnsw_index.bin").write_bytes(b"x")
    return d


def _manager(tmp_path, registered_aliases):
    m = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    gr = tmp_path / "golden"
    gr.mkdir()
    m.golden_repos_dir = str(gr)
    m.list_golden_repos = MagicMock(  # type: ignore[method-assign]
        return_value=[{"alias": a} for a in registered_aliases]
    )
    m._sqlite_backend = MagicMock()
    return m, gr


class TestRegistryReconcileFromFilesystem:
    def test_backfills_indexed_repo_missing_from_registry(self, tmp_path):
        m, gr = _manager(tmp_path, registered_aliases=["repoB"])
        _mk_indexed_repo(gr, "repoA")  # indexed, NOT registered -> backfill
        _mk_indexed_repo(gr, "repoB")  # indexed, already registered -> skip
        (gr / "repoC").mkdir()  # not indexed -> ignore

        counts = m.reconcile_registry_from_filesystem()

        assert m._sqlite_backend.add_repo.call_count == 1, (
            "must backfill exactly the one indexed-but-unregistered repo"
        )
        assert m._sqlite_backend.add_repo.call_args.kwargs["alias"] == "repoA"
        assert m._sqlite_backend.add_repo.call_args.kwargs["clone_path"] == str(
            gr / "repoA"
        )
        assert counts["backfilled"] == 1
        assert counts["skipped_registered"] == 1
        assert counts["indexed"] == 2

    def test_does_not_backfill_dirs_without_a_semantic_index(self, tmp_path):
        m, gr = _manager(tmp_path, registered_aliases=[])
        (gr / "repoC" / ".code-indexer").mkdir(parents=True)  # dir, but no index
        (gr / "plain-dir").mkdir()

        counts = m.reconcile_registry_from_filesystem()

        m._sqlite_backend.add_repo.assert_not_called()
        assert counts["backfilled"] == 0
        assert counts["indexed"] == 0

    def test_tolerates_concurrent_backfill_integrityerror(self, tmp_path):
        # A sibling pod backfilled the same repo first -> add_repo raises
        # IntegrityError; the reconcile must not abort.
        m, gr = _manager(tmp_path, registered_aliases=[])
        _mk_indexed_repo(gr, "repoA")
        _mk_indexed_repo(gr, "repoD")
        m._sqlite_backend.add_repo.side_effect = sqlite3.IntegrityError(
            "already exists"
        )

        counts = m.reconcile_registry_from_filesystem()  # must not raise

        assert counts["errors"] == 2
        assert counts["backfilled"] == 0
