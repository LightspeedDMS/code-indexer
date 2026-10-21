"""The e2e catalog world never leaves a TRACKED change in the live catalog.

On a live server whose catalog is a git repository (the backup bootstrap
makes it one and commits ``.gitignore``), any tracked change the world
leaves in the golden working tree is copied by an activation's CoW clone,
which then logs "unexpected TRACKED changes" and fails the phase log audit.
These tests drive the real seed/restore helpers against a real git catalog
shaped like the one a live server holds after the backup bootstrap.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List

from tests.e2e.cidx_meta_world import restore_catalog, seed_catalog, snapshot_catalog

_IDENTITY = ["-c", "user.name=e2e", "-c", "user.email=e2e@example.com"]
# What the backup bootstrap commits as the catalog's .gitignore.
_BOOTSTRAP_GITIGNORE = ".code-indexer/\n.snapshot-reader-leases/\n"
_SERVER_PATTERN = Path("xray-patterns") / "alpha-global" / "sample-pattern.yaml"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout


def _tracked_catalog(tmp_path: Path) -> Path:
    root = tmp_path / "cidx-meta"
    root.mkdir()
    (root / ".gitignore").write_text(_BOOTSTRAP_GITIGNORE, encoding="utf-8")
    (root / "cidx-meta.md").write_text("# cidx-meta\n", encoding="utf-8")
    (root / "_domains.json").write_text("[]\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "master")
    _git(root, "add", "-A")
    _git(root, *_IDENTITY, "commit", "-qm", "auto: initial cidx-meta state")
    return root


def _tracked_changes(repo: Path) -> List[str]:
    """The activation's own check: refresh the index, then the tracked
    lines of ``git status --porcelain``."""
    subprocess.run(
        ["git", "update-index", "-q", "--refresh"],
        cwd=repo,
        capture_output=True,
        timeout=60,
    )
    status = _git(repo, "status", "--porcelain")
    return [line for line in status.splitlines() if not line.startswith("??")]


def test_seeding_changes_no_tracked_catalog_file(tmp_path: Path) -> None:
    root = _tracked_catalog(tmp_path)

    seed_catalog(root)

    assert _tracked_changes(root) == []
    assert (root / ".gitignore").read_text(encoding="utf-8") == _BOOTSTRAP_GITIGNORE


def test_activation_copy_of_a_seeded_catalog_is_clean(tmp_path: Path) -> None:
    root = _tracked_catalog(tmp_path)
    seed_catalog(root)

    clone = tmp_path / "activated" / "cidx-meta"
    shutil.copytree(root, clone, symlinks=True)

    assert _tracked_changes(clone) == []


def test_restore_after_a_server_commit_leaves_no_tracked_change(
    tmp_path: Path,
) -> None:
    root = _tracked_catalog(tmp_path)
    snapshot = snapshot_catalog(root)
    seed_catalog(root)
    # The server commits a pattern stored through its front door
    # (xray_pattern_service commits exactly the stored file).
    pattern = root / _SERVER_PATTERN
    pattern.parent.mkdir(parents=True)
    pattern.write_text("name: sample-pattern\n", encoding="utf-8")
    _git(root, "add", "--", str(_SERVER_PATTERN))
    _git(root, *_IDENTITY, "commit", "-qm", "xray-patterns: add sample")

    restore_catalog(root, snapshot)

    assert not pattern.exists()
    assert _tracked_changes(root) == []
    assert snapshot_catalog(root) == snapshot


def test_restore_of_an_untracked_catalog_needs_no_git(tmp_path: Path) -> None:
    # Phases 3 and 6: the catalog is a plain directory, not a repository.
    root = tmp_path / "cidx-meta"
    root.mkdir()
    (root / "cidx-meta.md").write_text("# cidx-meta\n", encoding="utf-8")
    snapshot = snapshot_catalog(root)
    seed_catalog(root)

    restore_catalog(root, snapshot)

    assert snapshot_catalog(root) == snapshot
    assert not (root / ".git").exists()
