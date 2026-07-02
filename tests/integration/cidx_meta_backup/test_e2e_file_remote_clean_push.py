"""Integration test for Story #926 AC2 with a real file:// remote."""

import subprocess
from pathlib import Path


def _clone(remote: Path, destination: Path) -> None:
    subprocess.run(
        ["git", "clone", remote.as_uri(), str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_e2e_file_remote_clean_push(tmp_path):
    """# Story #926 AC2: local changes sync successfully to a bare file:// remote."""
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
    (repo / "local.txt").write_text("change\n")

    result = CidxMetaBackupSync(str(repo), "master", None).sync()

    assert result.skipped is False
    assert result.sync_failure is None
    clone = tmp_path / "verify"
    _clone(remote, clone)
    assert (clone / "local.txt").read_text() == "change\n"
