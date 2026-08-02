"""
Tests for Bug #1514 (activation self-heal gap): the fix for stale-path
post-checkout hooks was wired into `smart_indexer.py`'s indexing flow
(`GitHookManager.ensure_hook_installed()`), but never into
`ActivatedRepoManager`. A CoW clone of an activated repository copies
`.git/hooks/post-checkout` byte-for-byte from the golden repo's own clone
-- if that hook predates the #1514 fix (or was itself inherited from yet
another machine), it bakes in a stale, install-time absolute path. When
`_do_activate_repository` immediately runs
`git checkout -B <branch> origin/<branch>` for a non-default-branch
activation, the stale hook fires BEFORE any indexing has ever run on the
activated repo's own copy -- so `ensure_hook_installed()`'s self-heal
(only wired into the indexing flow) never gets a chance to repair it.

These tests exercise a new `ActivatedRepoManager._ensure_branch_hook_self_heal`
helper directly against REAL git repositories (no mocking of git or of
GitHookManager) -- proving:
  (a) an old-style stale hook is healed, checkout runs cleanly afterwards,
      and a real metadata.json at the correct relative location gets
      correctly updated by the healed (dynamic-path) hook.
  (b) a repo with no existing hook gets one installed (first-activation
      case).
  (c) a repo whose hook is ALREADY dynamic-path is left byte-identical
      (no redundant reinstall).
"""

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.services.git_hook_manager import GitHookManager
from unittest.mock import MagicMock


def _make_manager(data_dir: str) -> ActivatedRepoManager:
    """Minimal ActivatedRepoManager backed by a temp filesystem dir."""
    golden_repo_manager = MagicMock()
    golden_repo_manager.golden_repos = {}
    background_job_manager = MagicMock()
    return ActivatedRepoManager(
        data_dir=data_dir,
        golden_repo_manager=golden_repo_manager,
        background_job_manager=background_job_manager,
    )


def _run_git(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _init_repo_with_two_branches(repo_dir: Path) -> None:
    """Real git repo with 'main' and 'feature' branches, each with a commit."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    assert _run_git(["init", "-b", "main"], cwd=str(repo_dir)).returncode == 0
    assert (
        _run_git(
            ["config", "user.email", "test@example.com"], cwd=str(repo_dir)
        ).returncode
        == 0
    )
    assert _run_git(["config", "user.name", "Test"], cwd=str(repo_dir)).returncode == 0

    (repo_dir / "file.txt").write_text("main content\n")
    assert _run_git(["add", "file.txt"], cwd=str(repo_dir)).returncode == 0
    assert (
        _run_git(["commit", "-m", "initial on main"], cwd=str(repo_dir)).returncode == 0
    )

    assert _run_git(["checkout", "-b", "feature"], cwd=str(repo_dir)).returncode == 0
    (repo_dir / "file.txt").write_text("feature content\n")
    assert _run_git(["add", "file.txt"], cwd=str(repo_dir)).returncode == 0
    assert (
        _run_git(["commit", "-m", "commit on feature"], cwd=str(repo_dir)).returncode
        == 0
    )

    assert _run_git(["checkout", "main"], cwd=str(repo_dir)).returncode == 0


def _write_real_metadata(repo_dir: Path, current_branch: str) -> Path:
    metadata_dir = repo_dir / ".code-indexer"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    metadata_file = metadata_dir / "metadata.json"
    metadata_file.write_text(
        '{"current_branch": "%s", "provider": "voyage-ai"}' % current_branch
    )
    return metadata_file


def _write_stale_hook(repo_dir: Path, wrong_metadata_path: Path) -> Path:
    """Old-style hook: bakes in a WRONG absolute metadata path, no dynamic
    resolution, no error handling -- an unhandled PermissionError from a
    chmod-000 target surfaces exactly like the real staging incident's
    raw traceback.
    """
    hooks_dir = repo_dir / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_file = hooks_dir / "post-checkout"
    hook_content = f"""#!/bin/bash

# Code Indexer Branch Tracking
if [ "$3" = "1" ]; then
    CURRENT_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "unknown")
    python3 -c "
import json
from pathlib import Path
metadata_file = Path('{wrong_metadata_path}')
with open(metadata_file, 'r+') as f:
    data = json.load(f)
    data['current_branch'] = '$CURRENT_BRANCH'
    f.seek(0)
    f.truncate()
    json.dump(data, f, indent=2)
"
fi
"""
    hook_file.write_text(hook_content)
    hook_file.chmod(0o755)
    return hook_file


class TestStaleHookSelfHealOnActivation:
    def test_stale_hook_is_healed_and_checkout_updates_metadata_correctly(
        self, tmp_path
    ) -> None:
        """
        RED->GREEN: an old-style stale-path hook (inherited byte-for-byte
        from a CoW clone of a different machine's golden repo) must be
        healed by the new helper BEFORE any checkout runs, so that:
          (i) the healed hook file contains the dynamic-path marker,
          (ii) a real `git checkout` runs cleanly -- no PermissionError,
          (iii) the metadata.json at the CORRECT relative location is
               genuinely updated by the healed hook (proving dynamic-path
               correctness, not just "no crash").
        """
        repo_dir = tmp_path / "activated-repo"
        _init_repo_with_two_branches(repo_dir)
        metadata_file = _write_real_metadata(repo_dir, current_branch="main")

        # A WRONG absolute path (simulating a leaked path from a different
        # machine) pointing at a file we cannot write to.
        wrong_dir = tmp_path / "wrong-machine" / ".code-indexer"
        wrong_dir.mkdir(parents=True)
        wrong_metadata_file = wrong_dir / "metadata.json"
        wrong_metadata_file.write_text('{"current_branch": "main"}')
        wrong_metadata_file.chmod(0o000)

        hook_file = _write_stale_hook(repo_dir, wrong_metadata_file)

        # Prove the OLD hook is genuinely broken: checking out 'feature'
        # right now would surface the raw PermissionError from the
        # unhealed hook (mirrors the real staging incident's traceback).
        pre_heal_checkout = _run_git(["checkout", "feature"], cwd=str(repo_dir))
        combined_output = pre_heal_checkout.stdout + pre_heal_checkout.stderr
        assert "PermissionError" in combined_output
        # Restore chmod so cleanup of tmp_path doesn't choke, and go back
        # to main so the "real" post-heal checkout below is meaningful.
        wrong_metadata_file.chmod(0o644)
        _run_git(["checkout", "main"], cwd=str(repo_dir))

        # Re-stale the wrong file for the real proof below (heal must
        # prevent the hook from ever touching it again).
        wrong_metadata_file.chmod(0o000)

        data_dir = str(tmp_path / "server-data")
        manager = _make_manager(data_dir)

        # GREEN target: this helper does not exist yet on current code.
        manager._ensure_branch_hook_self_heal(repo_dir)

        healed_content = hook_file.read_text()
        assert GitHookManager._DYNAMIC_PATH_MARKER in healed_content

        # Now the actual activation-style checkout must run cleanly.
        post_heal_checkout = _run_git(["checkout", "feature"], cwd=str(repo_dir))
        assert post_heal_checkout.returncode == 0
        combined_output_2 = post_heal_checkout.stdout + post_heal_checkout.stderr
        assert "PermissionError" not in combined_output_2

        # The REAL metadata.json (correct relative location) must be
        # genuinely updated -- proving dynamic-path correctness.
        import json

        updated = json.loads(metadata_file.read_text())
        assert updated["current_branch"] == "feature"

        # Cleanup permission bit so tmp_path teardown doesn't fail.
        wrong_metadata_file.chmod(0o644)

    def test_no_existing_hook_gets_installed_and_updates_metadata(
        self, tmp_path
    ) -> None:
        """First-activation case: no post-checkout hook exists at all."""
        repo_dir = tmp_path / "activated-repo-fresh"
        _init_repo_with_two_branches(repo_dir)
        metadata_file = _write_real_metadata(repo_dir, current_branch="main")

        hook_file = repo_dir / ".git" / "hooks" / "post-checkout"
        assert not hook_file.exists()

        data_dir = str(tmp_path / "server-data")
        manager = _make_manager(data_dir)

        manager._ensure_branch_hook_self_heal(repo_dir)

        assert hook_file.exists()
        assert GitHookManager._DYNAMIC_PATH_MARKER in hook_file.read_text()

        result = _run_git(["checkout", "feature"], cwd=str(repo_dir))
        assert result.returncode == 0

        import json

        updated = json.loads(metadata_file.read_text())
        assert updated["current_branch"] == "feature"

    def test_already_dynamic_hook_is_left_untouched(self, tmp_path) -> None:
        """A hook that already contains the dynamic-path marker must not
        be reinstalled -- exact byte content and mtime unchanged.
        """
        repo_dir = tmp_path / "activated-repo-already-healed"
        _init_repo_with_two_branches(repo_dir)
        _write_real_metadata(repo_dir, current_branch="main")

        # Install a genuinely dynamic-path hook via the real GitHookManager
        # (the production hook-generation code), so the fixture is a
        # faithful "already healed" hook rather than a hand-rolled stand-in.
        real_metadata_path = repo_dir / ".code-indexer" / "metadata.json"
        ghm = GitHookManager(repo_path=repo_dir, metadata_file=real_metadata_path)
        ghm.install_branch_change_hook()

        hook_file = repo_dir / ".git" / "hooks" / "post-checkout"
        before_bytes = hook_file.read_bytes()
        before_mtime_ns = hook_file.stat().st_mtime_ns

        data_dir = str(tmp_path / "server-data")
        manager = _make_manager(data_dir)

        manager._ensure_branch_hook_self_heal(repo_dir)

        after_bytes = hook_file.read_bytes()
        after_mtime_ns = hook_file.stat().st_mtime_ns

        assert after_bytes == before_bytes
        assert after_mtime_ns == before_mtime_ns


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
