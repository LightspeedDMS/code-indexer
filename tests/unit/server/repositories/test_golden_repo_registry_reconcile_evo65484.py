"""issue-65484: registry reconcile — backfill golden repos that are indexed on disk
but missing from the registry.

Registration persists the index before the registry row, so an interrupted job
(pod eviction / crash) leaves index-without-entry; the workspace-mcp seeder then
sees the repo as unregistered and re-provisions it (issue-65481). This reconcile
heals the drift by backfilling a registry row for every indexed-on-disk repo that
has no entry — never re-indexing, idempotent, tolerant of concurrent sibling pods.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services import global_activation_repair

_REPAIR = (
    "code_indexer.server.services.global_activation_repair.repair_global_activation"
)


@pytest.fixture(autouse=True)
def _no_activation_repair():
    """The passes under test here are the backfill and the prune; the
    activation repair (issue-64616) has its own tests below and in
    test_global_activation_repair_evo64616.py."""
    with patch(
        _REPAIR, return_value=global_activation_repair.ActivationRepairResult()
    ) as fake:
        yield fake


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


def _manager_rows(tmp_path, rows):
    """Like _manager but list_golden_repos returns full rows (with clone_path),
    so the bidirectional prune pass can be exercised."""
    m = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    gr = tmp_path / "golden"
    gr.mkdir(exist_ok=True)
    m.golden_repos_dir = str(gr)
    m.list_golden_repos = MagicMock(return_value=rows)  # type: ignore[method-assign]
    m._sqlite_backend = MagicMock()
    m.is_globally_active = MagicMock(return_value=False)  # type: ignore[method-assign]
    # No per-user activated repos derive from these aliases by default (a bare
    # MagicMock would return a truthy Mock and wrongly block every prune).
    m.activated_repo_manager = MagicMock()
    m.activated_repo_manager.find_repos_by_golden_alias.return_value = []
    return m, gr


class TestRegistryReconcilePrunesOrphanRows:
    """issue-65484 Gap 3: prune registry rows whose in-tree clone dir has vanished."""

    def test_prunes_row_whose_clone_dir_is_gone(self, tmp_path):
        m, gr = _manager_rows(
            tmp_path,
            [{"alias": "gone", "clone_path": str(tmp_path / "golden" / "gone")}],
        )  # note: no dir created on disk for "gone"

        counts = m.reconcile_registry_from_filesystem()

        m._sqlite_backend.remove_repo.assert_called_once_with("gone")
        assert counts["pruned_orphan_rows"] == 1

    def test_does_not_prune_when_clone_dir_present(self, tmp_path):
        m, gr = _manager_rows(
            tmp_path,
            [{"alias": "live", "clone_path": str(tmp_path / "golden" / "live")}],
        )
        (gr / "live").mkdir()  # dir present -> not an orphan

        counts = m.reconcile_registry_from_filesystem()

        m._sqlite_backend.remove_repo.assert_not_called()
        assert counts["pruned_orphan_rows"] == 0

    def test_does_not_prune_external_clone_path(self, tmp_path):
        # clone_path outside <golden_repos_dir> -> we can't vouch for that mount.
        ext = tmp_path / "elsewhere" / "extrepo"
        m, gr = _manager_rows(tmp_path, [{"alias": "extrepo", "clone_path": str(ext)}])

        counts = m.reconcile_registry_from_filesystem()

        m._sqlite_backend.remove_repo.assert_not_called()
        assert counts["pruned_orphan_rows"] == 0

    def test_skips_orphan_row_with_derived_activated_repos(self, tmp_path):
        # dir gone, but a per-user activated repo still derives from this golden —
        # must NOT prune (remove_golden_repo cascades; this background prune won't).
        m, gr = _manager_rows(
            tmp_path,
            [{"alias": "shared", "clone_path": str(tmp_path / "golden" / "shared")}],
        )
        m.activated_repo_manager.find_repos_by_golden_alias.return_value = [
            {"username": "alice", "user_alias": "shared", "is_composite": False}
        ]

        counts = m.reconcile_registry_from_filesystem()

        m._sqlite_backend.remove_repo.assert_not_called()
        assert counts["orphan_rows_skipped_activated"] == 1
        assert counts["pruned_orphan_rows"] == 0

    def test_skips_globally_active_orphan_row(self, tmp_path):
        m, gr = _manager_rows(
            tmp_path,
            [{"alias": "gactive", "clone_path": str(tmp_path / "golden" / "gactive")}],
        )  # dir gone, but globally active
        m.is_globally_active = MagicMock(return_value=True)  # type: ignore[method-assign]

        counts = m.reconcile_registry_from_filesystem()

        m._sqlite_backend.remove_repo.assert_not_called()
        assert counts["orphan_rows_skipped_global"] == 1
        assert counts["pruned_orphan_rows"] == 0


class TestReconcileActivationEvo64616:
    """issue-64616: the backfill raced in-flight adds (the add then failed on the
    duplicate key before activating) and never activated what it backfilled,
    stranding golden rows with no -global entry."""

    def test_does_not_backfill_an_alias_whose_add_is_in_flight(self, tmp_path):
        m, gr = _manager(tmp_path, registered_aliases=[])
        _mk_indexed_repo(gr, "inflight")
        _mk_indexed_repo(gr, "crashed")
        m.job_tracker = MagicMock()
        m.job_tracker.active_job_id.side_effect = lambda op, alias: (
            "job-1" if (op, alias) == ("add_golden_repo", "inflight") else None
        )
        with patch.object(
            m, "_reconstruct_repo_identity", return_value=("https://g/x.git", "main")
        ):
            counts = m.reconcile_registry_from_filesystem()

        assert [
            c.kwargs["alias"] for c in m._sqlite_backend.add_repo.call_args_list
        ] == ["crashed"]
        assert counts["skipped_in_flight"] == 1
        assert counts["backfilled"] == 1

    def test_an_unanswerable_job_lookup_skips_the_backfill(self, tmp_path):
        m, gr = _manager(tmp_path, registered_aliases=[])
        _mk_indexed_repo(gr, "repoA")
        m.job_tracker = MagicMock()
        m.job_tracker.active_job_id.side_effect = RuntimeError("db down")

        counts = m.reconcile_registry_from_filesystem()

        m._sqlite_backend.add_repo.assert_not_called()
        assert counts["errors"] == 1

    def test_runs_the_repair_on_the_current_rows_and_merges_counts(
        self, tmp_path, _no_activation_repair
    ):
        m, _ = _manager(tmp_path, registered_aliases=["repoB"])
        result = global_activation_repair.ActivationRepairResult(
            candidates=["repoB"], activated=["repoB"]
        )
        _no_activation_repair.return_value = result

        counts = m.reconcile_registry_from_filesystem()

        _no_activation_repair.assert_called_once_with(m, [{"alias": "repoB"}])
        assert counts["activated_global"] == 1
        assert counts["activation_candidates"] == 1

    def test_a_failed_repair_is_counted_not_raised(
        self, tmp_path, _no_activation_repair
    ):
        m, _ = _manager(tmp_path, registered_aliases=[])
        _no_activation_repair.side_effect = RuntimeError("registry down")

        counts = m.reconcile_registry_from_filesystem()

        assert counts["errors"] == 1


class TestActivateGlobally:
    def _activate(self, tmp_path, published):
        m = GoldenRepoManager(data_dir=str(tmp_path / "data"))
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as activator_cls:
            m._activate_globally(
                alias="steps",
                repo_url="https://g/steps.git",
                clone_path="/golden/steps",
                enable_temporal=False,
                temporal_options=None,
                published=published,
            )
        return activator_cls.return_value.activate_golden_repo.call_args.kwargs

    def test_records_the_published_version_with_the_pointer(self, tmp_path):
        kwargs = self._activate(tmp_path, {"index_version": "v1"})
        assert kwargs["alias_extra"] == {"index_version": "v1"}
        assert kwargs["repo_name"] == "steps"

    def test_nothing_published_writes_a_plain_pointer(self, tmp_path):
        assert "alias_extra" not in self._activate(tmp_path, None)


class _Aliases:
    """AliasManager stand-in: which -global pointers were deleted / exist."""

    def __init__(self, deleted=(), existing=(), error=None):
        self._deleted = set(deleted)
        self._existing = set(existing)
        self._error = error

    def deleted_at(self, name):
        if self._error is not None:
            raise self._error
        return "2026-10-18T04:01:20.000000Z" if name in self._deleted else None

    def alias_exists(self, name):
        return name in self._existing

    def list_records(self):
        """No rename in progress (issue-64616 rename guard)."""
        return {}


class TestBackfillSkipsAClonesARemovalLeft:
    """issue-64616: a removal that left its clone was undone by the backfill."""

    def _reconcile(self, tmp_path, aliases):
        m, gr = _manager(tmp_path, registered_aliases=[])
        _mk_indexed_repo(gr, "removed")
        _mk_indexed_repo(gr, "crashed")
        _mk_indexed_repo(gr, "readded")
        with (
            patch(
                "code_indexer.global_repos.alias_manager.AliasManager",
                return_value=aliases,
            ),
            patch.object(
                m,
                "_reconstruct_repo_identity",
                return_value=("https://g/x.git", "main"),
            ),
        ):
            counts = m.reconcile_registry_from_filesystem()
        backfilled = [
            c.kwargs["alias"] for c in m._sqlite_backend.add_repo.call_args_list
        ]
        return counts, backfilled

    def test_skips_only_the_removed_clone(self, tmp_path):
        aliases = _Aliases(
            deleted={"removed-global", "readded-global"},
            existing={"readded-global"},
        )
        counts, backfilled = self._reconcile(tmp_path, aliases)

        assert backfilled == ["crashed", "readded"]
        assert counts["skipped_removed"] == 1

    def test_an_unanswerable_question_skips_and_counts(self, tmp_path):
        counts, backfilled = self._reconcile(
            tmp_path, _Aliases(error=RuntimeError("registry down"))
        )

        assert backfilled == []
        assert counts["errors"] == 3


class TestMetaDescriptionHook:
    def test_calls_the_add_hook(self, tmp_path):
        m = GoldenRepoManager(data_dir=str(tmp_path / "data"))
        with patch(
            "code_indexer.global_repos.meta_description_hook.on_repo_added"
        ) as hook:
            m._run_meta_description_hook("steps", "https://g/steps.git", "/g/steps")
        assert hook.call_args.kwargs["repo_name"] == "steps"
        assert hook.call_args.kwargs["clone_path"] == "/g/steps"

    def test_a_hook_failure_is_logged_not_raised(self, tmp_path, caplog):
        m = GoldenRepoManager(data_dir=str(tmp_path / "data"))
        with patch(
            "code_indexer.global_repos.meta_description_hook.on_repo_added",
            side_effect=RuntimeError("claude cli down"),
        ):
            m._run_meta_description_hook("steps", "https://g/steps.git", "/g/steps")
        assert "Meta description hook failed for 'steps'" in caplog.text
