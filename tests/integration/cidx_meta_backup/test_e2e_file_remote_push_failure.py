"""Integration test for Story #926 AC6 deferred push failure."""

import subprocess


def test_e2e_file_remote_push_failure(tmp_path):
    """# Story #926 AC6: push failure is reported in SyncResult without aborting the sync call."""
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

    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    result = CidxMetaBackupSync(str(repo), "master", None).sync()

    assert result.skipped is False
    assert result.sync_failure is not None
    assert result.sync_failure.startswith("push failed:")
