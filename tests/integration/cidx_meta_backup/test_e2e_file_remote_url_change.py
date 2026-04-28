"""Integration test for Story #926 AC9 remote URL changes."""

import subprocess


def test_e2e_file_remote_url_change(tmp_path):
    """# Story #926 AC9: re-running bootstrap with a new URL updates origin and force-pushes there."""
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    remote_one = tmp_path / "one.git"
    remote_two = tmp_path / "two.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote_one)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "init", "--bare", str(remote_two)], check=True, capture_output=True
    )

    repo = tmp_path / "cidx-meta"
    repo.mkdir()
    (repo / "README.md").write_text("seed\n")

    bootstrap = CidxMetaBackupBootstrap()
    assert bootstrap.bootstrap(str(repo), remote_one.as_uri()) == "bootstrapped"
    assert bootstrap.bootstrap(str(repo), remote_two.as_uri()) == "already_initialized"

    origin_url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert origin_url == remote_two.as_uri()

    clone = tmp_path / "verify"
    subprocess.run(
        ["git", "clone", remote_two.as_uri(), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (clone / "README.md").read_text() == "seed\n"
