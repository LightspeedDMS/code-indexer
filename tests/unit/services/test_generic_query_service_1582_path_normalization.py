"""TDD tests for Bug #1582 -- GenericQueryService._is_result_current_branch()
must normalize an ABSOLUTE stored ``payload.path`` before comparing it
against ``branch_context["files"]`` (always repo-relative, sourced from
``git ls-tree``).

ROOT CAUSE CLASS: same bug class as Bug #1575 Part A (which fixed two
sibling un-normalized path-comparison sites in
``high_throughput_processor.py`` and ``hnsw_index_manager.py`` via the
shared ``_normalize_stored_path_for_visibility`` helper). This is a THIRD
site Part A's own AC6 didn't cover.

RED phase: the tests below must FAIL against pre-fix
``_is_result_current_branch()`` (a raw ``file_path in branch_context["files"]``
membership check that never matches an absolute stored path against the
repo-relative git-tracked file set) and PASS once the fix normalizes the
stored path via the shared helper before the membership check.

The discriminating input is an ABSOLUTE stored path for a file that DOES
exist in the current branch -- a relative-only input would pass on both
correct and broken code (this project's TDD RED discipline: the test must
fail on the buggy code specifically because of the un-normalized
comparison, not for an unrelated reason).
"""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from code_indexer.config import Config
from code_indexer.services.generic_query_service import GenericQueryService
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 16


def _vector(seed: int = 0):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist()


@pytest.fixture
def real_git_repo(tmp_path):
    """A real git repository with one tracked file, `src/main.py`."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text('print("main")')

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "src/main.py"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


class TestAbsoluteStoredPathMatchesCurrentBranch:
    """Bug #1582: an absolute stored payload.path for a git-tracked,
    currently-visible file must be recognized as belonging to the current
    branch, not silently dropped.
    """

    def test_absolute_stored_path_matches_relative_branch_files(self, real_git_repo):
        query_service = GenericQueryService(
            real_git_repo,
            Config(
                codebase_dir=real_git_repo,
                file_extensions=["py"],
                exclude_dirs=[],
            ),
        )

        # Real branch context, sourced from a real `git ls-tree` call --
        # always repo-relative.
        branch_context = query_service._get_current_branch_context()
        assert "src/main.py" in branch_context["files"]

        # The stored result's path is ABSOLUTE (the exact shape confirmed
        # live in issue #1582 -- a collection whose payload.path values are
        # absolute).
        absolute_path = str(real_git_repo / "src" / "main.py")
        result = {
            "payload": {
                "path": absolute_path,
                "git_available": True,
            }
        }

        assert (
            query_service._is_result_current_branch(result, branch_context) is True
        ), (
            "An absolute stored path for a git-tracked, currently-visible "
            "file must match the current branch's relative file set, not "
            "be silently dropped."
        )

    def test_absolute_stored_path_through_real_filesystem_vector_store(
        self, real_git_repo
    ):
        """End-to-end reproduction: index a collection with an absolute
        stored payload.path against a REAL FilesystemVectorStore, retrieve
        the point exactly as the query path would see it, and confirm
        filter_results_by_branch() no longer drops it.
        """
        index_dir = real_git_repo / ".code-indexer" / "index"
        store = FilesystemVectorStore(base_path=index_dir, project_root=real_git_repo)
        store.create_collection("coll", vector_size=VECTOR_DIM)
        store.begin_indexing("coll")

        absolute_path = str(real_git_repo / "src" / "main.py")
        store.upsert_points(
            "coll",
            [
                {
                    "id": "point_1",
                    "vector": _vector(0),
                    "payload": {
                        "path": absolute_path,
                        "type": "content",
                        "git_available": True,
                    },
                }
            ],
        )
        store.end_indexing("coll")

        points, _ = store.scroll_points("coll", limit=10)
        assert len(points) == 1
        stored_result = points[0]
        assert stored_result["payload"]["path"] == absolute_path

        query_service = GenericQueryService(
            real_git_repo,
            Config(
                codebase_dir=real_git_repo,
                file_extensions=["py"],
                exclude_dirs=[],
            ),
        )

        filtered = query_service.filter_results_by_branch([stored_result])

        assert len(filtered) == 1, (
            "The vector store correctly holds the matching result, but "
            "filter_results_by_branch() silently dropped it because the "
            "absolute stored path was never normalized against the "
            "repo-relative git-tracked file set."
        )


class TestRelativeProjectDirHardening:
    """Codex review follow-up (post-approval, dual-review round): when
    ``GenericQueryService`` is constructed with a RELATIVE ``project_dir``,
    ``_normalize_stored_path_for_visibility``'s
    ``Path(path).relative_to(project_root)`` call raises ``ValueError`` for
    an ABSOLUTE stored path (an absolute path can never be "under" a
    relative root), falls into the except-branch, and returns the path
    UNCHANGED -- so the #1582 fix above does not actually apply for this
    input shape. Reproduced live before hardening:
    ``GenericQueryService(Path("repo"), ...)`` with an absolute stored
    payload path still returned ``False`` from
    ``_is_result_current_branch()``.

    Every current call site (``cli.py:1345``, ``cli.py:6947``) passes
    ``config.codebase_dir``, believed always-already-absolute in practice,
    so this is a latent robustness gap rather than a live production bug --
    still worth closing via a small, targeted hardening:
    ``GenericQueryService.__init__`` resolves ``project_dir`` to an
    absolute path before storing it.
    """

    def test_relative_project_dir_with_absolute_stored_path_still_matches(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "repo" / "src").mkdir(parents=True)
        (tmp_path / "repo" / "src" / "main.py").write_text('print("main")')

        subprocess.run(
            ["git", "init"],
            cwd=tmp_path / "repo",
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=tmp_path / "repo",
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=tmp_path / "repo",
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "add", "src/main.py"],
            cwd=tmp_path / "repo",
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=tmp_path / "repo",
            check=True,
            capture_output=True,
        )

        # Change into tmp_path so a RELATIVE Path("repo") resolves (via the
        # process cwd) to the real repo above -- the discriminating input
        # per the coordinator's report.
        monkeypatch.chdir(tmp_path)
        relative_project_dir = Path("repo")
        assert not relative_project_dir.is_absolute()

        query_service = GenericQueryService(
            relative_project_dir,
            Config(
                codebase_dir=relative_project_dir,
                file_extensions=["py"],
                exclude_dirs=[],
            ),
        )

        branch_context = query_service._get_current_branch_context()
        assert "src/main.py" in branch_context["files"]

        absolute_path = str((tmp_path / "repo" / "src" / "main.py").resolve())
        result = {
            "payload": {
                "path": absolute_path,
                "git_available": True,
            }
        }

        assert (
            query_service._is_result_current_branch(result, branch_context) is True
        ), (
            "A GenericQueryService constructed with a RELATIVE project_dir "
            "must still correctly normalize an ABSOLUTE stored path against "
            "the current branch's relative file set."
        )
