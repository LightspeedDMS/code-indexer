"""The X-Ray filename-mode walk never yields a file under a ``.git`` segment,
at any depth (the repository's own ``.git`` and a nested one such as a
submodule's), while ``.gitignore``-style names stay ordinary candidates.

Drives the real ``XRaySearchEngine._run_phase1_filename`` walk over a real
directory tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_languages")

from code_indexer.xray.search_engine import XRaySearchEngine  # noqa: E402


def test_filename_walk_skips_every_git_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    files = [
        ".git/config",
        "sub/.git/config",
        "sub/deeper/.git/HEAD",
        "src/config.py",
        ".gitignore",
    ]
    for rel in files:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("example\n")

    engine = XRaySearchEngine()
    candidates = engine._run_phase1_filename(repo, r".*", [], [])

    found = sorted(str(p.relative_to(repo)) for p in candidates)
    assert found == [".gitignore", "src/config.py"]
