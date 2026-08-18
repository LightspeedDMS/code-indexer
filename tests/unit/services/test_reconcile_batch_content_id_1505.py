"""Tests for Issue #1505: --reconcile spawns O(N) git subprocess calls.

Bug: `_do_reconcile_with_database` computes each unchanged file's "effective
content ID" via `_get_effective_content_id_for_reconcile` -> `_get_file_commit`,
which spawns ONE `git log -1 --format=%H -- <path>` subprocess PER FILE. For a
repo with tens of thousands of files this is tens of thousands of serial
subprocess spawns.

Fix: batch-fetch every tracked file's committed blob hash in ONE `git ls-tree
-r HEAD` subprocess call, and compare it against the already-schema-supported
`git_blob_hash` payload field (populated during real indexing) instead of the
per-file last-touching-commit hash. The working-directory-dirty subset keeps
using the pre-existing (Bug #471 batched) mtime/size identity scheme
unchanged.

These tests exercise `_do_reconcile_with_database` against a REAL git
repository (real `git init`/commits, zero git mocking) with a lightweight
in-memory fake vector-store client, so the git-log-per-file explosion is
observed for real, not inferred.
"""

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch


from code_indexer.config import Config
from code_indexer.services.high_throughput_processor import BranchIndexingResult
from code_indexer.services.smart_indexer import SmartIndexer


# ---------------------------------------------------------------------------
# Git test-repo helpers (real git, never mocked)
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _blob_hash_at_head(root: Path, rel_path: str) -> str:
    return _git(root, "rev-parse", f"HEAD:{rel_path}")


# ---------------------------------------------------------------------------
# Fake in-memory vector store client
# ---------------------------------------------------------------------------


def _condition_matches(point: Dict[str, Any], condition: Dict[str, Any]) -> bool:
    key = condition["key"]
    match = condition["match"]
    value = point.get("id") if key == "id" else point.get("payload", {}).get(key)
    if "value" in match:
        return bool(value == match["value"])
    if "any" in match:
        if isinstance(value, list):
            return any(v in match["any"] for v in value)
        return bool(value in match["any"])
    return False


def _filter_matches(
    point: Dict[str, Any], filter_conditions: Optional[Dict[str, Any]]
) -> bool:
    if not filter_conditions:
        return True
    for condition in filter_conditions.get("must", []):
        if not _condition_matches(point, condition):
            return False
    for condition in filter_conditions.get("must_not", []):
        if _condition_matches(point, condition):
            return False
    return True


class FakeVectorStoreClient:
    """Minimal in-memory fake sufficient to drive `_do_reconcile_with_database`.

    Not a mock of git or of the code under test -- a real, controllable
    substitute for the storage backend (an "in-memory implementation", the
    #2 preference in the mocking hierarchy) so the reconcile DECISION logic
    can be exercised deterministically against a real git repo.
    """

    def __init__(self) -> None:
        self.points: List[Dict[str, Any]] = []
        self.scroll_call_count = 0

    def add_committed_point(
        self,
        rel_path: str,
        blob_hash: Optional[str],
        commit_hash: str = "deadbeef",
        chunk_index: int = 0,
        hidden_branches: Optional[List[str]] = None,
    ) -> None:
        self.points.append(
            {
                "id": f"{rel_path}:{chunk_index}",
                "payload": {
                    "type": "content",
                    "path": rel_path,
                    "git_blob_hash": blob_hash,
                    "git_commit_hash": commit_hash,
                    "file_mtime": 1000.0,
                    "file_size": 10,
                    "hidden_branches": hidden_branches or [],
                },
            }
        )

    def add_working_dir_point(
        self, rel_path: str, mtime: float, size: int, chunk_index: int = 0
    ) -> None:
        self.points.append(
            {
                "id": f"{rel_path}:working_dir:{chunk_index}",
                "payload": {
                    "type": "content",
                    "path": rel_path,
                    "filesystem_mtime": mtime,
                    "file_size": size,
                    "file_mtime": mtime,
                },
            }
        )

    # -- vector_store_client interface used by reconcile -------------------

    def ensure_provider_aware_collection(self, config, embedding_provider, quiet):
        return "test_collection"

    def resolve_collection_name(self, config, embedding_provider):
        return "test_collection"

    def begin_indexing(self, collection_name):
        return None

    def end_indexing(self, collection_name, progress_callback=None):
        return {"vectors_indexed": 0}

    def collection_exists(self, collection_name):
        return False

    def delete_by_filter(self, collection_name, filter_conditions):
        return True

    def scroll_points(
        self,
        filter_conditions: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        offset: Optional[Any] = None,
        with_payload: bool = True,
        with_vectors: bool = False,
        collection_name: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Any]]:
        # Codex #1505 review Finding 1: count every scroll_points call so a
        # test can assert reconcile's branch-visibility check no longer
        # issues one call per already-visible file.
        self.scroll_call_count += 1
        matched = [p for p in self.points if _filter_matches(p, filter_conditions)]
        return matched[:limit], None


# ---------------------------------------------------------------------------
# Indexer construction / reconcile-execution helpers
# ---------------------------------------------------------------------------


def _make_indexer(codebase_dir: Path, vector_store_client: Any) -> SmartIndexer:
    config = Config(codebase_dir=str(codebase_dir))
    embedding_provider = MagicMock()
    metadata_path = codebase_dir.parent / "metadata.json"
    return SmartIndexer(
        config=config,
        embedding_provider=embedding_provider,
        vector_store_client=vector_store_client,
        metadata_path=metadata_path,
    )


def _run_reconcile(indexer: SmartIndexer) -> Dict[str, List[str]]:
    """Run `_do_reconcile_with_database`, capturing which files were sent for
    re-indexing without doing any real embedding/chunking work."""
    captured: Dict[str, List[str]] = {}

    def fake_branch_changes(**kwargs):
        captured["changed_files"] = list(kwargs["changed_files"])
        captured["unchanged_files"] = list(kwargs["unchanged_files"])
        return BranchIndexingResult(
            files_processed=len(kwargs["changed_files"]),
            content_points_created=0,
            cancelled=False,
            processing_time=0.001,
        )

    with (
        patch.object(
            indexer,
            "process_branch_changes_high_throughput",
            side_effect=fake_branch_changes,
        ),
        patch.object(indexer, "_cleanup_multiple_visible_content_points"),
    ):
        indexer._do_reconcile_with_database(
            batch_size=50,
            progress_callback=None,
            git_status={"git_available": True},
            provider_name="test-provider",
            model_name="test-model",
            quiet=True,
            vector_thread_count=2,
        )

    return {
        "changed_files": captured.get("changed_files", []),
        "unchanged_files": captured.get("unchanged_files", []),
    }


def _write_file(root: Path, rel_path: str, content: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# Test 1: O(1) subprocess calls, not O(N) -- the core discriminating test
# ---------------------------------------------------------------------------


class TestReconcileSubprocessCallCount:
    def test_unchanged_files_use_o1_git_subprocess_calls(self, tmp_path):
        """With N unchanged, already-indexed files, reconcile must issue a
        small CONSTANT number of git subprocess calls from smart_indexer.py's
        own content-id computation path -- never one `git log` per file.

        This test fails against the pre-fix implementation (N subprocess
        calls for N unchanged files) and passes once the batched blob-hash
        lookup replaces the per-file `git log -1 -- path` call.
        """
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)

        num_files = 60
        rel_paths = [f"src/file_{i}.py" for i in range(num_files)]
        for rel_path in rel_paths:
            _write_file(root, rel_path, f"# content {rel_path}\n")
        _commit_all(root, "initial commit")

        vector_store = FakeVectorStoreClient()
        for rel_path in rel_paths:
            blob_hash = _blob_hash_at_head(root, rel_path)
            vector_store.add_committed_point(rel_path, blob_hash)

        indexer = _make_indexer(root, vector_store)

        real_subprocess_run = subprocess.run
        call_count = 0
        git_log_call_count = 0

        def counting_run(cmd, *args, **kwargs):
            nonlocal call_count, git_log_call_count
            call_count += 1
            if isinstance(cmd, list) and cmd[:2] == ["git", "log"]:
                git_log_call_count += 1
            return real_subprocess_run(cmd, *args, **kwargs)

        with patch(
            "code_indexer.services.smart_indexer.subprocess.run",
            side_effect=counting_run,
        ):
            result = _run_reconcile(indexer)

        # Nothing should have been re-indexed -- everything is unchanged.
        assert result["changed_files"] == []

        # THE precise fix: the exact O(N) offender from Issue #1505
        # (`git log -1 --format=%H -- path`, one call per unchanged file)
        # must be issued ZERO times when every file's blob hash is resolved
        # via the batched `git ls-tree -r HEAD` map.
        assert git_log_call_count == 0, (
            f"Expected zero per-file `git log` calls for {num_files} unchanged "
            f"files (all resolvable via the batched blob-hash map), but "
            f"observed {git_log_call_count} -- the O(N) per-file git log "
            f"subprocess call has regressed."
        )

        # Secondary assertion: a small constant number of TOTAL git
        # invocations, never anything close to num_files. The bound of 6
        # covers: is_git_available() (1, memoized), get_current_branch() (1),
        # _get_modified_files_set() (2: unstaged + staged diff), and
        # _get_head_blob_hash_map() (1: the new batched ls-tree call) --
        # all one-time-per-run overhead, never per-file.
        assert call_count <= 6, (
            f"Expected O(1) git subprocess calls (<=6) for {num_files} unchanged "
            f"files, but observed {call_count} -- indicates a per-file git "
            f"subprocess call has regressed back to O(N)."
        )


# ---------------------------------------------------------------------------
# Test 2/3/5/6: correctness of the classification decisions
# ---------------------------------------------------------------------------


class TestReconcileCorrectness:
    def test_unchanged_files_are_not_reindexed(self, tmp_path):
        """Files whose stored blob hash matches HEAD's committed blob hash
        must be classified as up-to-date and skipped."""
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)

        _write_file(root, "a.py", "print('a')\n")
        _write_file(root, "b.py", "print('b')\n")
        _commit_all(root, "initial")

        vector_store = FakeVectorStoreClient()
        vector_store.add_committed_point("a.py", _blob_hash_at_head(root, "a.py"))
        vector_store.add_committed_point("b.py", _blob_hash_at_head(root, "b.py"))

        indexer = _make_indexer(root, vector_store)
        result = _run_reconcile(indexer)

        assert result["changed_files"] == []

    def test_file_changed_via_new_commit_is_reindexed(self, tmp_path):
        """A file whose content changed in a NEW commit (different blob hash)
        must be identified as needing re-indexing."""
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)

        _write_file(root, "a.py", "print('a')\n")
        _write_file(root, "b.py", "print('b')\n")
        _commit_all(root, "initial")

        vector_store = FakeVectorStoreClient()
        # Seed DB with the ORIGINAL (pre-change) blob hash for a.py.
        old_blob_hash = _blob_hash_at_head(root, "a.py")
        vector_store.add_committed_point("a.py", old_blob_hash)
        vector_store.add_committed_point("b.py", _blob_hash_at_head(root, "b.py"))

        # Now change a.py's content and commit again -- blob hash changes.
        _write_file(root, "a.py", "print('a changed')\n")
        _commit_all(root, "modify a.py")

        indexer = _make_indexer(root, vector_store)
        result = _run_reconcile(indexer)

        assert "a.py" in result["changed_files"]
        assert "b.py" not in result["changed_files"]

    def test_new_never_indexed_file_is_classified_as_new(self, tmp_path):
        """A file that exists on disk (and in git) but has no DB entry at all
        must be classified as new and sent for indexing."""
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)

        _write_file(root, "a.py", "print('a')\n")
        _write_file(root, "new_file.py", "print('new')\n")
        _commit_all(root, "initial")

        vector_store = FakeVectorStoreClient()
        vector_store.add_committed_point("a.py", _blob_hash_at_head(root, "a.py"))
        # new_file.py deliberately has NO db entry.

        indexer = _make_indexer(root, vector_store)
        result = _run_reconcile(indexer)

        assert "new_file.py" in result["changed_files"]
        assert "a.py" not in result["changed_files"]

    def test_deleted_file_is_cleaned_up(self, tmp_path):
        """A file present in the DB but removed from disk must trigger the
        existing deletion-cleanup path (delete_by_filter / branch-aware
        delete), independent of the content-id computation change."""
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)

        _write_file(root, "a.py", "print('a')\n")
        _commit_all(root, "initial")

        vector_store = FakeVectorStoreClient()
        vector_store.add_committed_point("a.py", _blob_hash_at_head(root, "a.py"))
        # Simulate a previously-indexed, now-deleted file.
        vector_store.add_committed_point("deleted.py", "0" * 40)

        indexer = _make_indexer(root, vector_store)

        with patch.object(
            indexer, "delete_file_branch_aware"
        ) as mock_delete_branch_aware:
            _run_reconcile(indexer)

        deleted_paths = [
            call.args[0] for call in mock_delete_branch_aware.call_args_list
        ]
        assert "deleted.py" in deleted_paths


# ---------------------------------------------------------------------------
# Test 4: working-directory-dirty file path must be byte-for-byte preserved
# ---------------------------------------------------------------------------


class TestReconcileWorkingDirDirtyUnaffected:
    def test_dirty_file_still_uses_mtime_size_identity(self, tmp_path):
        """A file with uncommitted local changes must still be classified via
        the existing (Bug #471 batched) working-directory mtime/size scheme,
        completely independent of the new blob-hash batching."""
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)

        _write_file(root, "a.py", "print('a')\n")
        _commit_all(root, "initial")

        vector_store = FakeVectorStoreClient()
        # DB holds the committed version's blob hash.
        vector_store.add_committed_point("a.py", _blob_hash_at_head(root, "a.py"))

        # Dirty the working directory WITHOUT committing.
        _write_file(root, "a.py", "print('a dirty')\n")

        indexer = _make_indexer(root, vector_store)
        result = _run_reconcile(indexer)

        # Dirty file differs from its committed blob -- must be reindexed,
        # exactly like before this change (working_dir identity scheme).
        assert "a.py" in result["changed_files"]

    def test_dirty_file_content_id_uses_working_dir_scheme(self, tmp_path):
        """Directly verify `_get_effective_content_id_for_reconcile` produces
        the untouched `working_dir_<mtime>_<size>` format for a dirty file,
        proving the dirty-file code path was not altered by this fix."""
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)

        _write_file(root, "a.py", "print('a')\n")
        _commit_all(root, "initial")
        _write_file(root, "a.py", "print('a dirty and longer')\n")

        vector_store = FakeVectorStoreClient()
        indexer = _make_indexer(root, vector_store)
        indexer._reconcile_modified_files = {"a.py"}
        indexer._reconcile_head_blob_hashes = {}

        content_id = indexer._get_effective_content_id_for_reconcile("a.py")

        assert content_id.startswith("a.py:working_dir_")


# ---------------------------------------------------------------------------
# Test 7: non-git repositories are completely unaffected (regression guard)
# ---------------------------------------------------------------------------


class TestReconcileNonGitUnaffected:
    def test_non_git_repo_reconcile_byte_identical(self, tmp_path):
        """Non-git repos must never call the new batched git primitive and
        must keep using the mtime/size content-id scheme exactly as before."""
        root = tmp_path / "repo"
        root.mkdir()
        # Deliberately NOT a git repo.

        _write_file(root, "a.py", "print('a')\n")

        vector_store = FakeVectorStoreClient()
        indexer = _make_indexer(root, vector_store)

        assert indexer.git_topology_service.is_git_available() is False

        real_subprocess_run = subprocess.run
        call_count = 0

        def counting_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return real_subprocess_run(*args, **kwargs)

        with patch(
            "code_indexer.services.smart_indexer.subprocess.run",
            side_effect=counting_run,
        ):
            result = _run_reconcile(indexer)

        # New file, not yet in db -> should be classified as needing indexing.
        assert "a.py" in result["changed_files"]
        # Pre-existing baseline: `_get_modified_files_set` unconditionally
        # attempts exactly 2 `git diff` calls (unstaged + staged) regardless
        # of git availability -- this predates and is unrelated to this fix.
        # The NEW `_get_head_blob_hash_map` batch primitive (this fix's own
        # addition) must never fire for a non-git repo, so the count must
        # stay exactly at that pre-existing baseline, never grow to 3.
        assert call_count == 2, (
            f"Expected the pre-existing baseline of exactly 2 git subprocess "
            f"calls for a non-git repo, got {call_count} -- the new batched "
            f"HEAD blob-hash primitive must never run when git is unavailable."
        )


# ---------------------------------------------------------------------------
# Test 8: edge case -- untracked-but-previously-committed file falls back
# gracefully to the per-file git log computation instead of being silently
# misclassified.
# ---------------------------------------------------------------------------


class TestReconcileEdgeCaseGracefulFallback:
    def test_untracked_via_rm_cached_falls_back_to_per_file_git_log(self, tmp_path):
        """A file removed from the git index via `git rm --cached` (but left
        unchanged on disk, already indexed in the DB) has NO entry in `git
        ls-tree -r HEAD` and is not reported by `git diff --name-only HEAD`
        (git diff does not report untracked files). The batched blob-hash
        map therefore has a genuine gap for this file's path -- the fix must
        gracefully fall back to the single-file `git log -1 -- path`
        computation for JUST this file, matching pre-fix behavior exactly,
        rather than silently treating it as unchanged or crashing.
        """
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)

        _write_file(root, "a.py", "print('a')\n")
        _commit_all(root, "initial")

        # Untrack a.py from git's index while leaving its on-disk content
        # byte-for-byte unchanged.
        _git(root, "rm", "--cached", "-q", "a.py")
        _git(root, "commit", "-q", "-m", "untrack a.py")

        # This is exactly what the OLD (pre-fix) per-file `_get_file_commit`
        # would have computed for this path: `git log` walks path history
        # regardless of current tracked status, so the most recent commit
        # touching "a.py" is the untrack commit itself (it rewrote the tree
        # entry), NOT the original "initial" commit.
        expected_fallback_commit = _git(root, "log", "-1", "--format=%H", "--", "a.py")

        vector_store = FakeVectorStoreClient()
        # DB holds the content id exactly as the OLD per-file git-log scheme
        # would have produced it at the time it was indexed (commit-hash
        # based) -- no committed tree entry exists for this path any more,
        # so this is the value a correct graceful fallback must reproduce.
        vector_store.add_committed_point(
            "a.py", blob_hash=None, commit_hash=expected_fallback_commit
        )
        # Simulate the legacy DB payload having no git_blob_hash field
        # recorded (only git_commit_hash) for this now-untracked path.
        vector_store.points[-1]["payload"].pop("git_blob_hash")

        indexer = _make_indexer(root, vector_store)

        real_subprocess_run = subprocess.run
        git_log_calls = []

        def counting_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[:2] == ["git", "log"]:
                git_log_calls.append(cmd)
            return real_subprocess_run(cmd, **kwargs)

        with patch(
            "code_indexer.services.smart_indexer.subprocess.run",
            side_effect=counting_run,
        ):
            result = _run_reconcile(indexer)

        # The graceful fallback fired exactly once for this one gap file
        # (never zero -- that would mean the file was silently treated as
        # unchanged without ever checking; never more than once -- that
        # would mean the O(1) batch path was bypassed more broadly).
        assert len(git_log_calls) == 1

        # The graceful fallback must reproduce the OLD per-file git-log
        # behavior exactly: current_effective_id, computed via the single
        # -file fallback, matches the DB's stored (equally fallback-derived)
        # commit hash -> file correctly recognized as up-to-date, NOT
        # spuriously re-indexed nor silently skipped due to a crash/exception.
        assert "a.py" not in result["changed_files"]


# ---------------------------------------------------------------------------
# Test 9 (Codex #1505 review, Finding 3): `_derive_db_content_id_from_point`
# must identify a "working directory" (mtime/size-identified) content point
# via an authoritative payload field, never via a substring match on the
# point id. Real point ids are deterministic UUID5 strings (see
# `git_aware_processor.py`/`high_throughput_processor.py`'s
# `_create_point_id`) and NEVER contain "working_dir" -- the pre-fix
# `"working_dir" in str(point.get("id", ""))` check was dead code against
# real persisted data. These tests use a REALISTIC persisted point shape
# (a real uuid5 id, and the exact payload keys
# `high_throughput_processor.py`'s `_create_vector_point` actually writes
# for a non-git-available file: `filesystem_mtime`/`filesystem_size`, no
# `git_blob_hash`/`git_commit_hash`), never the synthetic
# `a.py:working_dir:0` id shape the earlier tests in this file used.
# ---------------------------------------------------------------------------


class TestReconcileDirtyPointIdentityRealisticPayload:
    def test_non_git_style_payload_uses_mtime_size_identity_not_id_substring(
        self, tmp_path
    ):
        """A content point persisted for a non-git (mtime/size-identified)
        file has a UUID5 point id (no "working_dir" substring) and a payload
        carrying `filesystem_mtime`/`filesystem_size` with NO git fields.
        `_derive_db_content_id_from_point` must recognize this via the
        payload shape and return the working-dir-style content id, not fall
        through to the blob/commit-hash branch (which would silently
        collapse to a useless `f"{path}:unknown"` constant for every such
        file, per Codex's Finding 3)."""
        import uuid

        root = tmp_path / "repo"
        root.mkdir()

        vector_store = FakeVectorStoreClient()
        indexer = _make_indexer(root, vector_store)

        real_point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "proj:filehash:0"))
        assert "working_dir" not in real_point_id

        point = {
            "id": real_point_id,
            "payload": {
                "type": "content",
                "path": "a.py",
                "filesystem_mtime": 1700000000.123,
                "filesystem_size": 42,
            },
        }

        content_id = indexer._derive_db_content_id_from_point("a.py", point)

        assert content_id == "a.py:working_dir:1700000000.123:42"

    def test_committed_payload_with_uuid_id_still_uses_blob_hash(self, tmp_path):
        """Regression guard: a normal committed git content point (real
        UUID5 id, `git_blob_hash` present, no `filesystem_mtime`) must still
        resolve via the blob-hash branch, unaffected by the payload-shape
        based dirty-point detection added for Finding 3."""
        import uuid

        root = tmp_path / "repo"
        root.mkdir()

        vector_store = FakeVectorStoreClient()
        indexer = _make_indexer(root, vector_store)

        real_point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "proj:abc123:0"))

        point = {
            "id": real_point_id,
            "payload": {
                "type": "content",
                "path": "a.py",
                "git_blob_hash": "abc123",
                "git_commit_hash": "deadbeef",
            },
        }

        content_id = indexer._derive_db_content_id_from_point("a.py", point)

        assert content_id == "a.py:blob:abc123"


# ---------------------------------------------------------------------------
# Test 10 (Codex #1505 review, Finding 1): the branch-visibility ("unhide")
# check in `_do_reconcile_with_database` must derive `hidden_branches` from
# the SAME bulk snapshot scroll `_get_indexed_files_snapshot` already
# performs, instead of issuing a fresh `scroll_points` call PER indexed file.
# ---------------------------------------------------------------------------


def _run_reconcile_with_unhide_patch(indexer: SmartIndexer) -> Dict[str, Any]:
    """Like `_run_reconcile`, but also captures every
    `_ensure_file_visible_in_branch_thread_safe` call so unhide correctness
    can be asserted without a full fake unhide implementation."""
    captured: Dict[str, Any] = {}
    unhide_calls: List[Any] = []

    def fake_branch_changes(**kwargs):
        captured["changed_files"] = list(kwargs["changed_files"])
        captured["unchanged_files"] = list(kwargs["unchanged_files"])
        return BranchIndexingResult(
            files_processed=len(kwargs["changed_files"]),
            content_points_created=0,
            cancelled=False,
            processing_time=0.001,
        )

    def fake_unhide(relative_file_path, current_branch, collection_name):
        unhide_calls.append((relative_file_path, current_branch, collection_name))

    with (
        patch.object(
            indexer,
            "process_branch_changes_high_throughput",
            side_effect=fake_branch_changes,
        ),
        patch.object(indexer, "_cleanup_multiple_visible_content_points"),
        patch.object(
            indexer,
            "_ensure_file_visible_in_branch_thread_safe",
            side_effect=fake_unhide,
        ),
    ):
        indexer._do_reconcile_with_database(
            batch_size=50,
            progress_callback=None,
            git_status={"git_available": True},
            provider_name="test-provider",
            model_name="test-model",
            quiet=True,
            vector_thread_count=2,
        )

    # `_do_reconcile_with_database` returns early (before ever calling
    # `process_branch_changes_high_throughput`) when there is nothing to
    # reindex, so `captured` may still be empty here -- default like
    # `_run_reconcile` does.
    return {
        "changed_files": captured.get("changed_files", []),
        "unchanged_files": captured.get("unchanged_files", []),
        "unhide_calls": unhide_calls,
    }


class TestReconcileBranchVisibilityBulkScroll:
    def test_unhide_check_uses_o1_scroll_calls_not_one_per_file(self, tmp_path):
        """With N already-visible, unchanged, committed files, reconcile's
        branch-visibility check must NOT issue one `scroll_points` call per
        file -- the whole point of Issue #1505's snapshot-based batching is
        defeated if a per-file DB query survives inside the same reconcile
        loop this fix targets."""
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)

        num_files = 50
        rel_paths = [f"src/file_{i}.py" for i in range(num_files)]
        for rel_path in rel_paths:
            _write_file(root, rel_path, f"# content {rel_path}\n")
        _commit_all(root, "initial commit")

        vector_store = FakeVectorStoreClient()
        for rel_path in rel_paths:
            blob_hash = _blob_hash_at_head(root, rel_path)
            # Visible in current branch (no hidden_branches) -- the common
            # case reconcile must not re-query the DB for.
            vector_store.add_committed_point(rel_path, blob_hash)

        indexer = _make_indexer(root, vector_store)

        result = _run_reconcile_with_unhide_patch(indexer)

        assert result["changed_files"] == []
        assert result["unhide_calls"] == []

        # A small constant bound, independent of num_files. The bulk
        # snapshot scroll (`_scroll_all_content_points`, paginated in
        # batches of 5000) accounts for the only expected calls here --
        # never one additional call per already-visible file.
        assert vector_store.scroll_call_count <= 3, (
            f"Expected O(1) scroll_points calls for {num_files} already-"
            f"visible files, but observed {vector_store.scroll_call_count} "
            f"-- the branch-visibility check has regressed to a per-file "
            f"DB query."
        )

    def test_hidden_file_is_still_correctly_unhidden_via_bulk_snapshot(self, tmp_path):
        """A file genuinely hidden in the current branch must still be
        detected and unhidden correctly when derived from the bulk snapshot
        data, proving the O(1) rewrite preserves correctness."""
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)

        _write_file(root, "a.py", "print('a')\n")
        _write_file(root, "b.py", "print('b')\n")
        _commit_all(root, "initial")

        current_branch = _git(root, "branch", "--show-current")

        vector_store = FakeVectorStoreClient()
        vector_store.add_committed_point(
            "a.py",
            _blob_hash_at_head(root, "a.py"),
            hidden_branches=[current_branch],
        )
        vector_store.add_committed_point("b.py", _blob_hash_at_head(root, "b.py"))

        indexer = _make_indexer(root, vector_store)

        result = _run_reconcile_with_unhide_patch(indexer)

        unhidden_paths = [call[0] for call in result["unhide_calls"]]
        assert unhidden_paths == ["a.py"]


# ---------------------------------------------------------------------------
# Test 11 (Codex #1505 review, Finding 2): a failed `git ls-tree -r HEAD`
# invocation must be logged LOUDLY (return code + stderr), never silently
# swallowed -- silently returning `{}` reintroduces the exact O(N) per-file
# `git log` stall Issue #1505 was filed to fix, with no visibility.
# ---------------------------------------------------------------------------


class TestHeadBlobHashMapFailureLogging:
    def test_nonzero_return_code_logs_warning_with_code_and_stderr(
        self, tmp_path, caplog
    ):
        import logging
        import subprocess as subprocess_module

        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)
        _write_file(root, "a.py", "print('a')\n")
        _commit_all(root, "initial")

        vector_store = FakeVectorStoreClient()
        indexer = _make_indexer(root, vector_store)

        failing_result = subprocess_module.CompletedProcess(
            args=["git", "ls-tree", "-r", "-z", "HEAD"],
            returncode=128,
            stdout="",
            stderr="fatal: not a valid object name HEAD\n",
        )

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.services.smart_indexer"
        ):
            with patch(
                "code_indexer.services.smart_indexer.subprocess.run",
                return_value=failing_result,
            ):
                mapping = indexer._get_head_blob_hash_map()

        assert mapping == {}
        warning_messages = [
            record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ]
        assert any("128" in msg for msg in warning_messages), (
            f"Expected a WARNING mentioning the git return code (128), got: "
            f"{warning_messages}"
        )
        assert any("not a valid object name" in msg for msg in warning_messages), (
            f"Expected a WARNING including the git stderr output, got: "
            f"{warning_messages}"
        )


# ---------------------------------------------------------------------------
# Test 12 (Codex #1505 review, Finding 5): malformed `git ls-tree` entries
# must be counted and logged, never silently dropped.
# ---------------------------------------------------------------------------


class TestHeadBlobHashMapMalformedEntryLogging:
    def test_malformed_entries_are_counted_and_logged(self, tmp_path, caplog):
        import logging
        import subprocess as subprocess_module

        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)
        _write_file(root, "a.py", "print('a')\n")
        _commit_all(root, "initial")

        vector_store = FakeVectorStoreClient()
        indexer = _make_indexer(root, vector_store)

        valid_entry = "100644 blob abc123def456\ta.py"
        malformed_no_tab = "this-line-has-no-tab-separator"
        malformed_short_meta = "100644\tb.py"  # only 2 whitespace-separated meta parts
        stdout = "\0".join([valid_entry, malformed_no_tab, malformed_short_meta]) + "\0"

        fake_result = subprocess_module.CompletedProcess(
            args=["git", "ls-tree", "-r", "-z", "HEAD"],
            returncode=0,
            stdout=stdout,
            stderr="",
        )

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.services.smart_indexer"
        ):
            with patch(
                "code_indexer.services.smart_indexer.subprocess.run",
                return_value=fake_result,
            ):
                mapping = indexer._get_head_blob_hash_map()

        # The one well-formed entry is still parsed correctly.
        assert mapping == {"a.py": "abc123def456"}

        warning_messages = [
            record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ]
        assert any("2" in msg for msg in warning_messages), (
            f"Expected a WARNING reporting 2 skipped malformed entries, got: "
            f"{warning_messages}"
        )


# ---------------------------------------------------------------------------
# Test 13 (Codex #1505 review, Finding 2): when the batched HEAD blob-hash
# map fails/comes back empty for a non-empty git repo, reconcile must log a
# LOUD warning/error making the O(N) per-file degradation observable,
# instead of silently eating hours in production (Messi Rule #13).
# ---------------------------------------------------------------------------


class TestReconcileDegradedFallbackLogging:
    def test_map_entirely_empty_for_nonempty_repo_logs_loud_degradation_warning(
        self, tmp_path, caplog
    ):
        import logging

        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)

        num_files = 20
        rel_paths = [f"src/file_{i}.py" for i in range(num_files)]
        for rel_path in rel_paths:
            _write_file(root, rel_path, f"# content {rel_path}\n")
        _commit_all(root, "initial commit")

        vector_store = FakeVectorStoreClient()
        for rel_path in rel_paths:
            blob_hash = _blob_hash_at_head(root, rel_path)
            vector_store.add_committed_point(rel_path, blob_hash)

        indexer = _make_indexer(root, vector_store)

        real_subprocess_run = subprocess.run

        def failing_ls_tree(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[:2] == ["git", "ls-tree"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="simulated failure\n"
                )
            return real_subprocess_run(cmd, *args, **kwargs)

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.services.smart_indexer"
        ):
            with patch(
                "code_indexer.services.smart_indexer.subprocess.run",
                side_effect=failing_ls_tree,
            ):
                _run_reconcile(indexer)

        # NOTE: when the batched blob-hash map fails entirely, the disk-side
        # effective id falls back to the per-file commit-hash scheme while
        # the DB-side id (read directly from the stored `git_blob_hash`
        # payload field) stays blob-hash-based -- a pre-existing, orthogonal
        # identity-scheme mismatch outside this fix's scope. This test only
        # asserts that the degradation itself is made LOUD and observable.
        degraded_messages = [
            record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
            and (
                "degrad" in record.message.lower()
                or "fallback" in record.message.lower()
            )
            and "reconcile" in record.message.lower()
        ]
        assert degraded_messages, (
            "Expected a loud WARNING/ERROR log documenting that reconcile "
            "degraded to the slow per-file git-log fallback for a "
            "non-empty repo whose batched blob-hash map failed entirely, "
            f"but found none. All warnings: "
            f"{[r.message for r in caplog.records if r.levelno >= logging.WARNING]}"
        )
