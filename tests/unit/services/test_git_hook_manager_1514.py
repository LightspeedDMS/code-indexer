"""
Tests for Bug #1514: branch-delta activation dereferences a foreign,
stale absolute path baked into a copied git hook.

Root cause:
    GitHookManager.install_branch_change_hook() bakes the ABSOLUTE,
    install-time path of the progressive-metadata file literally into
    the generated `.git/hooks/post-checkout` script (a Python one-liner
    containing `Path('<absolute-path-at-install-time>')`).

    This project creates repository copies via full-tree copy operations
    (CoW reflink `cp --reflink=auto -a` for golden-repo base clones and
    activated-repo clones, or an out-of-band filesystem copy used to seed
    a golden repo from an existing working tree) rather than `git clone`
    -- `git clone` would create a FRESH `.git/hooks` directory containing
    only the `.sample` files, but a raw filesystem copy carries the
    `.git/hooks/post-checkout` file over byte-for-byte, stale absolute
    path baked in and all.

    `GitHookManager.ensure_hook_installed()` only checks whether the
    "# Code Indexer Branch Tracking" marker string is present in the
    hook file -- it never verifies the embedded path is still correct
    for the copy's own location. So once a hook is baked with a path
    from wherever/whenever the source tree was first indexed, EVERY
    later copy (golden repo base clone -> activated-repo CoW clone ->
    versioned snapshot, ad infinitum) inherits and keeps the ORIGINAL
    stale path forever, and `git checkout` on any of those copies
    triggers the hook, which dereferences a directory that has nothing
    to do with the copy actually being operated on. On a git checkout
    called from a service account without permission to traverse the
    original location, this surfaces as an uncaught PermissionError:
    `Path.exists()` (called unguarded, before the hook's own broad
    OSError/IOError try/except) re-raises on EACCES (it only swallows
    ENOENT/ENOTDIR/EBADF/ELOOP) -- and since `git checkout`'s exit code
    is derived from `post-checkout`'s own exit code (verified
    empirically), the crash surfaces as a `git checkout` failure with a
    combined stderr of "Switched to a new branch '...'" (git's own,
    always-emitted informational line) followed by the Python
    traceback -- exactly matching GitHub issue #1514's reported log.

    This test suite reproduces the underlying defect deterministically
    with REAL git subprocesses and REAL filesystem copies (no mocking):
    a hook baked at location A, when copied wholesale to location B,
    keeps writing to A instead of B. It does not depend on any specific
    permission setup to demonstrate the defect -- the wrong-target write
    is itself the root-cause mechanism; a PermissionError is merely one
    possible symptom depending on what happens to live at the stale path
    on a given host.
"""

import json
import shutil
import subprocess
from pathlib import Path

from code_indexer.services.git_hook_manager import GitHookManager


def _git(args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _init_repo_with_metadata(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    assert _git(["init", "-q"], repo_dir).returncode == 0
    assert _git(["config", "user.email", "test@example.com"], repo_dir).returncode == 0
    assert _git(["config", "user.name", "Test User"], repo_dir).returncode == 0

    (repo_dir / "file.txt").write_text("hello\n")
    assert _git(["add", "file.txt"], repo_dir).returncode == 0
    assert _git(["commit", "-q", "-m", "init"], repo_dir).returncode == 0

    # A second branch to check out later (created but not checked out yet,
    # so it survives the upcoming full-tree copy).
    assert _git(["branch", "feature"], repo_dir).returncode == 0

    metadata_dir = repo_dir / ".code-indexer"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = metadata_dir / "metadata.json"
    metadata_path.write_text(json.dumps({"current_branch": "main"}))


def _read_branch(metadata_path: Path) -> str:
    return json.loads(metadata_path.read_text())["current_branch"]


class TestGitHookManagerStalePathBug1514:
    """Reproduce and guard against the leaked-absolute-path hook defect."""

    def test_copied_hook_updates_original_location_not_the_copy(
        self, tmp_path: Path
    ) -> None:
        """
        RED: install the hook at a source location, copy the whole tree
        (simulating a CoW/reflink/rsync clone), call ensure_hook_installed()
        on the copy (exactly what activation code does before checkout),
        then perform a REAL `git checkout` in the COPY.

        Correct behaviour: the copy's OWN metadata.json is updated and the
        source's metadata.json is untouched (the hook must always operate
        on the directory it is actually running from).

        Buggy (current) behaviour: ensure_hook_installed() no-ops because
        the "# Code Indexer Branch Tracking" marker is already present, so
        the copy silently keeps the SOURCE's baked-in absolute path -- the
        post-checkout hook in the copy updates the SOURCE's metadata.json
        (a directory that, on a real server, belongs to a completely
        different activation/golden-repo/host context) and leaves the
        copy's own metadata.json un-updated.
        """
        source_repo = tmp_path / "source_repo"
        _init_repo_with_metadata(source_repo)

        source_metadata = source_repo / ".code-indexer" / "metadata.json"
        hook_manager_source = GitHookManager(source_repo, source_metadata)
        hook_manager_source.install_branch_change_hook()

        # Sanity: hook file exists.
        hook_file_source = source_repo / ".git" / "hooks" / "post-checkout"
        assert hook_file_source.exists()

        # Simulate a CoW reflink / rsync / raw filesystem copy: the WHOLE
        # tree, including .git/hooks/post-checkout, travels byte-for-byte.
        copy_repo = tmp_path / "copy_repo"
        shutil.copytree(source_repo, copy_repo)

        copy_metadata = copy_repo / ".code-indexer" / "metadata.json"
        assert _read_branch(copy_metadata) == "main"
        assert _read_branch(source_metadata) == "main"

        # This is exactly what ActivatedRepoManager does before every
        # branch checkout / branch-delta reindex: ensure the hook is
        # installed for the repo's OWN (current) location.
        hook_manager_copy = GitHookManager(copy_repo, copy_metadata)
        hook_manager_copy.ensure_hook_installed()

        # Real git checkout inside the COPY -- must trigger post-checkout.
        result = _git(["checkout", "feature"], copy_repo)
        assert result.returncode == 0, (
            f"git checkout unexpectedly failed: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )

        # The COPY's own metadata file must reflect the branch switch.
        assert _read_branch(copy_metadata) == "feature", (
            "post-checkout hook in the COPY updated the wrong file -- it "
            "dereferenced a stale, foreign absolute path instead of the "
            "copy's own metadata.json (Bug #1514)."
        )

        # The SOURCE's metadata file must be untouched by a checkout that
        # happened entirely inside the COPY.
        assert _read_branch(source_metadata) == "main", (
            "post-checkout hook in the COPY wrote into the SOURCE "
            "repository's metadata.json -- this is the exact 'foreign, "
            "hardcoded-looking absolute path' defect reported in issue "
            "#1514 (on a real server this resolves into a directory "
            "belonging to a different host/activation entirely, "
            "surfacing as PermissionError)."
        )

    def test_ensure_hook_installed_repairs_stale_hook_from_a_copy(
        self, tmp_path: Path
    ) -> None:
        """
        ensure_hook_installed() must actively repair (not just tolerate) a
        hook file that was inherited from a copy of a different directory,
        even when the "# Code Indexer Branch Tracking" marker is already
        present -- the marker alone does not mean the embedded path is
        still valid for THIS repo's current location.
        """
        source_repo = tmp_path / "source_repo2"
        _init_repo_with_metadata(source_repo)
        source_metadata = source_repo / ".code-indexer" / "metadata.json"
        GitHookManager(source_repo, source_metadata).install_branch_change_hook()

        copy_repo = tmp_path / "copy_repo2"
        shutil.copytree(source_repo, copy_repo)
        copy_metadata = copy_repo / ".code-indexer" / "metadata.json"

        hook_manager_copy = GitHookManager(copy_repo, copy_metadata)
        hook_manager_copy.ensure_hook_installed()

        hook_content = (copy_repo / ".git" / "hooks" / "post-checkout").read_text()

        # The repaired hook must not contain the SOURCE repo's own absolute
        # path baked in anywhere -- that is precisely the leak this bug
        # report is about.
        assert str(source_repo) not in hook_content, (
            "ensure_hook_installed() left the source repo's stale absolute "
            "path baked into the copy's hook file (Bug #1514)."
        )
