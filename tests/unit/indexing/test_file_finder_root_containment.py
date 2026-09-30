"""Server-context ``FileFinder`` candidates must resolve to a location
inside the codebase root.

Containment applies to server context (``Config.confine_to_codebase_root``);
local CLI context is covered by test_symlink_containment_context.py.

``FileFinder.find_files()``'s directory walk, size check, and text
sniffing all operate on a symlink's TARGET (following it), so a
candidate's resolved location must be checked separately from its own
name/extension before it is yielded for indexing.

These tests drive the REAL ``FileFinder.find_files()`` over real temporary
directories containing real symlinks (CLAUDE.md Foundation #1: no
filesystem mocks).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from code_indexer.config import Config
from code_indexer.indexing.file_finder import FileFinder
from code_indexer.utils.path_confinement import (
    is_resolved_within_root as _real_is_resolved_within_root,
)


def _make_finder(codebase_dir: Path) -> FileFinder:
    config = Config(codebase_dir=codebase_dir)
    config.confine_to_codebase_root()
    return FileFinder(config)


def _relative_paths(finder: FileFinder) -> set:
    found = set()
    for file_path in finder.find_files():
        found.add(str(file_path.relative_to(finder.config.codebase_dir)))
    return found


class TestFullWalkRootContainment:
    def test_symlink_to_file_outside_root_is_not_yielded(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "legit.py").write_text("# legit\n")

        outside_file = tmp_path / "outside_file.py"
        outside_file.write_text("MARKER_OUTSIDE_ROOT_CONTENT\n")
        escape_link = root / "escape_link.py"
        escape_link.symlink_to(outside_file)

        finder = _make_finder(root)
        found = _relative_paths(finder)

        assert "escape_link.py" not in found, (
            f"A symlink resolving outside the codebase root must not be "
            f"yielded by find_files(). Found: {found}"
        )
        assert "legit.py" in found

    def test_symlink_to_file_inside_root_is_still_yielded(self, tmp_path: Path) -> None:
        """No regression: an in-repo symlink keeps today's behavior --
        it is a legitimate, common pattern and must still be indexed."""
        root = tmp_path / "repo"
        root.mkdir()
        real_target = root / "real.py"
        real_target.write_text("# real content\n")
        inside_link = root / "inside_link.py"
        inside_link.symlink_to(real_target)

        finder = _make_finder(root)
        found = _relative_paths(finder)

        assert "inside_link.py" in found, (
            f"A symlink resolving inside the codebase root must still be "
            f"yielded (no regression). Found: {found}"
        )

    def test_symlinked_directory_pointing_outside_is_not_traversed(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "legit.py").write_text("# legit\n")

        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "nested_file.py").write_text("MARKER_OUTSIDE_DIR_CONTENT\n")
        linked_dir = root / "linked_dir"
        linked_dir.symlink_to(outside_dir, target_is_directory=True)

        finder = _make_finder(root)
        found = _relative_paths(finder)

        assert not any("nested_file.py" in f for f in found), (
            f"A symlinked directory pointing outside the codebase root "
            f"must never be traversed. Found: {found}"
        )

    def test_symlink_loop_is_skipped_without_raising(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "legit.py").write_text("# legit\n")
        loop_a = root / "loop_a.py"
        loop_b = root / "loop_b.py"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)

        finder = _make_finder(root)
        found = _relative_paths(finder)

        assert "legit.py" in found
        assert "loop_a.py" not in found
        assert "loop_b.py" not in found

    def test_broken_symlink_is_skipped_without_raising(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "legit.py").write_text("# legit\n")
        broken_link = root / "broken.py"
        broken_link.symlink_to(tmp_path / "does_not_exist.py")

        finder = _make_finder(root)
        found = _relative_paths(finder)

        assert "legit.py" in found
        assert "broken.py" not in found


class TestSymlinkedCodebaseDirNoRegression:
    """A symlinked codebase_dir (a supported real-world mount-path shape)
    must not drop any of its own, non-symlink files."""

    def test_symlinked_codebase_dir_still_yields_its_own_files(
        self, tmp_path: Path
    ) -> None:
        real_root = tmp_path / "real_repo"
        real_root.mkdir()
        for name in ("a", "b", "c"):
            (real_root / f"{name}.py").write_text(f"# file {name}\n")

        symlinked_root = tmp_path / "symlinked_repo"
        symlinked_root.symlink_to(real_root)

        finder = _make_finder(symlinked_root)
        found = _relative_paths(finder)

        assert found == {"a.py", "b.py", "c.py"}, (
            f"A symlinked codebase_dir must not drop any of its own "
            f"in-tree files. Found: {found}"
        )


class TestContainmentCheckOnlyRunsForSymlinks:
    """The resolve()-based containment check is expensive; it must only
    run for candidates that are themselves symlinks, never for every
    file walked."""

    def test_non_symlink_file_never_triggers_the_resolve_based_check(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "plain.py").write_text("print('plain')\n")

        finder = _make_finder(root)

        with patch(
            "code_indexer.indexing.file_finder.is_resolved_within_root"
        ) as mock_check:
            found = _relative_paths(finder)

        assert "plain.py" in found
        assert mock_check.call_count == 0, (
            "A non-symlink candidate must never trigger the resolve()-"
            f"based containment check. Call count: {mock_check.call_count}"
        )

    def test_symlink_file_still_triggers_the_resolve_based_check(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        real_target = root / "real.py"
        real_target.write_text("print('inside')\n")
        link = root / "link.py"
        link.symlink_to(real_target)

        finder = _make_finder(root)

        with patch(
            "code_indexer.indexing.file_finder.is_resolved_within_root",
            wraps=_real_is_resolved_within_root,
        ) as mock_check:
            found = _relative_paths(finder)

        assert "link.py" in found
        assert mock_check.call_count >= 1, (
            "A symlink candidate must still trigger the resolve()-based "
            "containment check."
        )
