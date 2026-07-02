"""Integration test for Story #926 AC3 with a real file:// remote."""

import subprocess
from pathlib import Path


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _clone(remote: Path, destination: Path) -> None:
    subprocess.run(
        ["git", "clone", remote.as_uri(), str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_e2e_file_remote_rebase(tmp_path):
    """# Story #926 AC3: sync rebases local work on top of remote drift, preserving both sides."""
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    repo = tmp_path / "cidx-meta"
    repo.mkdir()
    (repo / "README.md").write_text("seed\n")
    CidxMetaBackupBootstrap().bootstrap(str(repo), remote.as_uri())

    divergent = tmp_path / "divergent"
    _clone(remote, divergent)
    (divergent / "remote.txt").write_text("remote\n")
    _git(["add", "-A"], divergent)
    _git(["commit", "-m", "remote"], divergent)
    _git(["push", "origin", "master"], divergent)

    (repo / "local.txt").write_text("local\n")

    result = CidxMetaBackupSync(str(repo), "master", None).sync()

    assert result.skipped is False
    assert result.sync_failure is None
    verify = tmp_path / "verify"
    _clone(remote, verify)
    assert (verify / "remote.txt").read_text() == "remote\n"
    assert (verify / "local.txt").read_text() == "local\n"
