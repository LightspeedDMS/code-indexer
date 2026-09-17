"""Bug #1891 round 2 (S2): DirectoryExplorerService.generate_tree(path=...)
was not confined to the repository root.

``generate_tree`` computed ``start_path = self.repo_path / path`` and only
checked ``start_path.exists()`` -- a ``path`` containing parent-directory
segments (``"../sibling"``) or a same-repo symlink pointing outside the
repository escapes the repo root entirely and enumerates whatever
directory it resolves to. Reachable via the MCP ``directory_tree`` tool
(``code_indexer/server/mcp/handlers/files.py::handle_directory_tree``),
by any user who can call it on one repository.

The fix reuses the shared ``FileListingService.resolve_confined_path()``
primitive (round 1) to confine ``path`` before it is ever used to list a
directory, raising the SAME ``ValueError`` message the pre-existing
"path doesn't exist" check already produces -- so an escape attempt cannot
be distinguished from an ordinary nonexistent-path request (no existence
oracle).

Real filesystem operations throughout (CLAUDE.md Foundation #1) -- no
mocks.
"""

from __future__ import annotations

import os

import pytest

from code_indexer.global_repos.directory_explorer import DirectoryExplorerService

OUTSIDE_MARKER_FILE = "outside_secret.txt"
INSIDE_MARKER_FILE = "inside.py"


@pytest.fixture
def repo_with_sibling(tmp_path):
    """
    tmp_path/
      repo/                    <- repository root under test
        src/inside.py           <- legitimate nested file
        link_outside -> ../sibling   (symlink escaping the repo)
      sibling/
        outside_secret.txt       <- must NEVER be enumerated from repo's tree
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / INSIDE_MARKER_FILE).write_text("print('hi')")

    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / OUTSIDE_MARKER_FILE).write_text("top secret")

    os.symlink(sibling, repo / "link_outside")

    return repo, sibling


class TestGenerateTreeBlocksParentDirectoryEscape:
    def test_generate_tree_blocks_parent_directory_escape(self, repo_with_sibling):
        repo, _sibling = repo_with_sibling
        service = DirectoryExplorerService(repo)

        with pytest.raises(ValueError):
            service.generate_tree(path="../sibling")

    def test_error_message_matches_legitimate_nonexistent_path(self, repo_with_sibling):
        """The escape and the ordinary-missing-path branches share the
        exact same f"Path does not exist: {path}" template (verified by
        exact-matching against that template for the escape path, and by
        confirming the same template prefix on a genuinely missing path) --
        no extra security-specific detail (e.g. "outside repository",
        "PermissionError", "Access denied") is ever appended for the
        escape case, so it cannot be distinguished from an ordinary
        missing-path response."""
        repo, _sibling = repo_with_sibling
        service = DirectoryExplorerService(repo)

        with pytest.raises(ValueError) as escape_exc:
            service.generate_tree(path="../sibling")
        assert str(escape_exc.value) == "Path does not exist: ../sibling"

        with pytest.raises(ValueError) as missing_exc:
            service.generate_tree(path="does/not/exist")
        assert str(missing_exc.value) == "Path does not exist: does/not/exist"

        assert str(escape_exc.value).startswith("Path does not exist: ") and str(
            missing_exc.value
        ).startswith("Path does not exist: "), (
            "both branches must share the identical message template: "
            f"{escape_exc.value!r} vs {missing_exc.value!r}"
        )


class TestGenerateTreeBlocksSymlinkEscape:
    def test_generate_tree_blocks_symlink_escape(self, repo_with_sibling):
        repo, _sibling = repo_with_sibling
        service = DirectoryExplorerService(repo)

        with pytest.raises(ValueError):
            service.generate_tree(path="link_outside")

    def test_symlink_escape_error_message_matches_legitimate_nonexistent_path(
        self, repo_with_sibling
    ):
        """Same no-oracle property as the parent-directory-traversal case:
        the symlink-escape message is exactly the ordinary template with no
        extra security-specific detail appended."""
        repo, _sibling = repo_with_sibling
        service = DirectoryExplorerService(repo)

        with pytest.raises(ValueError) as escape_exc:
            service.generate_tree(path="link_outside")
        assert str(escape_exc.value) == "Path does not exist: link_outside"

        with pytest.raises(ValueError) as missing_exc:
            service.generate_tree(path="does/not/exist")
        assert str(missing_exc.value) == "Path does not exist: does/not/exist"

        assert str(escape_exc.value).startswith("Path does not exist: ") and str(
            missing_exc.value
        ).startswith("Path does not exist: "), (
            "both branches must share the identical message template: "
            f"{escape_exc.value!r} vs {missing_exc.value!r}"
        )


class TestGenerateTreeLegitimatePathsStillWork:
    def test_generate_tree_legitimate_subdirectory_still_works(self, repo_with_sibling):
        repo, _sibling = repo_with_sibling
        service = DirectoryExplorerService(repo)

        result = service.generate_tree(path="src")

        names = [c.name for c in result.root.children or []]
        assert INSIDE_MARKER_FILE in names

    def test_generate_tree_no_path_still_returns_full_repo_root(
        self, repo_with_sibling
    ):
        repo, _sibling = repo_with_sibling
        service = DirectoryExplorerService(repo)

        result = service.generate_tree()

        names = [c.name for c in result.root.children or []]
        assert "src" in names


class TestDiscriminationProof:
    """Proves the escape tests above are discriminating: the vulnerable
    pre-fix computation (repo_path / path, no confinement) genuinely
    resolves outside the repository and would enumerate the sibling
    directory's content."""

    def test_discrimination_proof_unconfined_start_path_would_reach_sibling(
        self, repo_with_sibling
    ):
        repo, _sibling = repo_with_sibling
        unconfined_start = (repo / "../sibling").resolve()

        assert unconfined_start.exists()
        assert (unconfined_start / OUTSIDE_MARKER_FILE).exists(), (
            "the vulnerable pre-fix code path (repo_path / path, no "
            "confinement) genuinely resolves outside the repository"
        )
