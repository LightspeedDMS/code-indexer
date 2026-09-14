"""Consolidated review finding H6 (Issue #1811/Bug #1812).

`_collect_graph_candidate_files` (src/code_indexer/server/mcp/handlers/
xray_graph.py) does `sorted(repo_path.rglob("*"))`, calls `.is_file()` on
EVERY entry the walk yields, and only THEN applies `_SKIP_DIR_NAMES` as a
post-hoc filter -- never a walk prune. On a repo with real `.git`/
`node_modules`/`.venv`/etc. content this means every single file inside
those directories is stat'd and then discarded. Correctly off the event
loop (`anyio.to_thread.run_sync`), so this is not a node-hang, but it burns
real wall-clock time -- production golden repos sit on hard NFSv3, where
each wasted stat costs ~5ms.

The fix (verified by this test) is to walk via `os.walk` and prune
`dirnames` IN PLACE so a skipped subtree is never descended into at all --
no stat call is ever made for anything inside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import patch

from code_indexer.server.mcp.handlers.xray_graph import _collect_graph_candidate_files


def _build_fixture(repo_root: Path) -> None:
    """A real repo file tree: a few genuine candidate files, plus a handful
    of files nested inside directories `_SKIP_DIR_NAMES` names."""
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "Main.java").write_text("class Main {}\n")
    (repo_root / "README.md").write_text("# readme\n")

    for skip_dir, nested in (
        (".git", ["HEAD", "objects/aa/bb", "refs/heads/main"]),
        ("node_modules", ["pkg/index.js", "pkg/lib/util.js"]),
        ("target", ["debug/build.log"]),
    ):
        for rel in nested:
            full = repo_root / skip_dir / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text("junk\n")


def test_walk_never_stats_anything_inside_a_skipped_directory(
    tmp_path: Path,
) -> None:
    """Discriminating test: records every path `Path.is_file()` is called
    on during the walk. A correct implementation that PRUNES skipped
    subtrees (os.walk + in-place dirnames filtering) never even visits
    those paths, so none of them can appear in the recorded call list --
    the current rglob-based implementation calls `.is_file()` on every
    entry under `.git`/`node_modules`/`target` BEFORE discarding it,
    which this test catches.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _build_fixture(repo_root)

    stated_paths: List[Path] = []
    real_is_file = Path.is_file

    def _tracking_is_file(self: Path) -> bool:
        stated_paths.append(self)
        return real_is_file(self)

    with patch.object(Path, "is_file", _tracking_is_file):
        results, _truncated = _collect_graph_candidate_files(
            repo_root, [], [], max_files=1000
        )

    for skip_dir in (".git", "node_modules", "target"):
        offending = [
            p for p in stated_paths if skip_dir in p.relative_to(repo_root).parts
        ]
        assert offending == [], (
            f"is_file() must never be called on anything inside "
            f"'{skip_dir}/' -- the walk must prune that subtree entirely, "
            f"but was called on: {offending}"
        )

    assert sorted(results) == ["README.md", "src/Main.java"], (
        f"expected only the two real candidate files, got: {results}"
    )
