"""Unit tests for Story #926 cidx-meta backup bootstrap."""

import os
import subprocess
from pathlib import Path


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_bare_remote(tmp_path: Path, name: str = "origin.git") -> Path:
    bare = tmp_path / name
    subprocess.run(
        ["git", "init", "--bare", str(bare)], check=True, capture_output=True
    )
    return bare


def _write_seed_files(repo_path: Path) -> None:
    (repo_path / "README.md").write_text("seed\n")
    (repo_path / ".code-indexer").mkdir()
    (repo_path / ".code-indexer" / "state.json").write_text("{}\n")


def test_bootstrap_creates_git_repo_when_no_git_dir(tmp_path):
    """# Story #926 AC1: bootstrap initializes git, commits, adds remote, and pushes."""
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    repo_path = tmp_path / "cidx-meta"
    repo_path.mkdir()
    _write_seed_files(repo_path)
    remote_path = _init_bare_remote(tmp_path)

    result = CidxMetaBackupBootstrap().bootstrap(str(repo_path), remote_path.as_uri())

    assert result == "bootstrapped"
    assert (repo_path / ".git").is_dir()
    assert _git(["remote", "get-url", "origin"], repo_path) == remote_path.as_uri()
    assert _git(["rev-list", "--count", "HEAD"], repo_path) == "1"

    clone_path = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", remote_path.as_uri(), str(clone_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (clone_path / "README.md").read_text() == "seed\n"


def test_bootstrap_idempotent_same_url(tmp_path):
    """# Story #926 AC1: bootstrap is idempotent when origin already matches."""
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    repo_path = tmp_path / "cidx-meta"
    repo_path.mkdir()
    _write_seed_files(repo_path)
    remote_path = _init_bare_remote(tmp_path)
    bootstrap = CidxMetaBackupBootstrap()
    assert bootstrap.bootstrap(str(repo_path), remote_path.as_uri()) == "bootstrapped"

    head_before = _git(["rev-parse", "HEAD"], repo_path)
    assert (
        bootstrap.bootstrap(str(repo_path), remote_path.as_uri())
        == "already_initialized"
    )
    assert _git(["rev-parse", "HEAD"], repo_path) == head_before


def test_bootstrap_updates_remote_on_url_change(tmp_path):
    """# Story #926 AC9: bootstrap updates origin URL and force-pushes to the new remote."""
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    repo_path = tmp_path / "cidx-meta"
    repo_path.mkdir()
    _write_seed_files(repo_path)
    remote_one = _init_bare_remote(tmp_path, "one.git")
    remote_two = _init_bare_remote(tmp_path, "two.git")
    bootstrap = CidxMetaBackupBootstrap()

    assert bootstrap.bootstrap(str(repo_path), remote_one.as_uri()) == "bootstrapped"
    assert (
        bootstrap.bootstrap(str(repo_path), remote_two.as_uri())
        == "already_initialized"
    )
    assert _git(["remote", "get-url", "origin"], repo_path) == remote_two.as_uri()

    clone_path = tmp_path / "clone-two"
    subprocess.run(
        ["git", "clone", remote_two.as_uri(), str(clone_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (clone_path / "README.md").read_text() == "seed\n"


def test_bootstrap_gitignore_excludes_code_indexer(tmp_path):
    """# Story #926 AC1: bootstrap writes .gitignore excluding .code-indexer/."""
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    repo_path = tmp_path / "cidx-meta"
    repo_path.mkdir()
    _write_seed_files(repo_path)
    remote_path = _init_bare_remote(tmp_path)

    CidxMetaBackupBootstrap().bootstrap(str(repo_path), remote_path.as_uri())

    gitignore = (repo_path / ".gitignore").read_text()
    assert ".code-indexer/" in gitignore


def test_bootstrap_uses_mutable_base_path(tmp_path):
    """# Story #926 AC1: bootstrap only mutates the provided golden repo path, never a snapshot path."""
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    mutable_path = tmp_path / "golden-repos" / "cidx-meta"
    mutable_path.mkdir(parents=True)
    versioned_path = tmp_path / ".versioned" / "cidx-meta" / "v_123"
    versioned_path.mkdir(parents=True)
    _write_seed_files(mutable_path)
    (versioned_path / "SHOULD_NOT_CHANGE").write_text("snapshot\n")
    remote_path = _init_bare_remote(tmp_path)

    CidxMetaBackupBootstrap().bootstrap(str(mutable_path), remote_path.as_uri())

    assert (mutable_path / ".git").is_dir()
    assert not (versioned_path / ".git").exists()
    assert (versioned_path / "SHOULD_NOT_CHANGE").read_text() == "snapshot\n"


def _seed_remote_with_commit(remote_uri: str, work_dir: Path) -> None:
    """Push a single commit directly to a bare remote to establish diverged history."""
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "seed",
        "GIT_AUTHOR_EMAIL": "seed@test.invalid",
        "GIT_COMMITTER_NAME": "seed",
        "GIT_COMMITTER_EMAIL": "seed@test.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    }
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "remote_seed.txt").write_text("remote\n")
    subprocess.run(["git", "init", str(work_dir)], check=True, capture_output=True)
    # Force initial branch to master so it matches bootstrap's fallback branch name.
    subprocess.run(
        ["git", "-C", str(work_dir), "symbolic-ref", "HEAD", "refs/heads/master"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_dir), "add", "-A"],
        check=True,
        capture_output=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "-C", str(work_dir), "commit", "-m", "remote seed"],
        check=True,
        capture_output=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "-C", str(work_dir), "push", remote_uri, "master"],
        check=True,
        capture_output=True,
    )


def test_push_raises_on_divergent_remote(tmp_path):
    """_push raises RuntimeError when push is non-fast-forward rejected (no force fallback)."""
    import pytest

    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    remote_path = _init_bare_remote(tmp_path)
    _seed_remote_with_commit(remote_path.as_uri(), tmp_path / "seed")

    repo_path = tmp_path / "cidx-meta"
    repo_path.mkdir()
    _write_seed_files(repo_path)

    with pytest.raises(RuntimeError, match="push rejected by remote"):
        CidxMetaBackupBootstrap().bootstrap(str(repo_path), remote_path.as_uri())


def test_push_raises_on_bad_remote(tmp_path):
    """bootstrap raises RuntimeError when push to unreachable remote fails."""
    import pytest

    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    repo_path = tmp_path / "cidx-meta"
    repo_path.mkdir()
    _write_seed_files(repo_path)

    bad_remote = str(tmp_path / "nonexistent" / "repo.git")
    with pytest.raises(RuntimeError, match="push rejected by remote"):
        CidxMetaBackupBootstrap().bootstrap(str(repo_path), bad_remote)
