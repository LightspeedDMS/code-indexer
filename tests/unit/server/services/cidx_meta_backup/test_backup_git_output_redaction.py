"""No backup git failure carries a credential out of the backup module.

A remote's messages and git's own argv can echo a remote URL whose
userinfo holds credentials (https://user:token@host/...). Everything the
bootstrap and the sync raise or return on failure -- and the refresh logs
or records -- has that userinfo masked.

Real git against real origins (a rejecting pre-receive hook that echoes a
URL with userinfo, and a duplicate ``git remote add``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.cidx_meta_backup.bootstrap import (
    BackupPushFailed,
    CidxMetaBackupBootstrap,
)
from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

SECRET = "example-secret-token-4567"
URL_WITH_USERINFO = f"https://example-user:{SECRET}@example.com/r.git"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@example.com", "-c", "user.name=a", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _echoing_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\necho 'refused; see {URL_WITH_USERINFO}' >&2\nexit 1\n")
    hook.chmod(0o755)
    return origin


def _catalog(tmp_path: Path) -> Path:
    meta = tmp_path / "cidx-meta"
    meta.mkdir()
    (meta / "example-repo.md").write_text("# example-repo\n")
    return meta


def test_a_bootstrap_push_failure_masks_the_remote_credential(tmp_path: Path) -> None:
    origin = _echoing_origin(tmp_path)

    with pytest.raises(BackupPushFailed) as raised:
        CidxMetaBackupBootstrap().bootstrap(str(_catalog(tmp_path)), origin.as_uri())

    assert raised.value.reason == "the remote rejected the push"
    assert "refused" in str(raised.value), "the masked detail was dropped"
    assert SECRET not in str(raised.value)


def test_a_failed_git_call_never_raises_the_remote_url_credential(
    tmp_path: Path,
) -> None:
    meta = _catalog(tmp_path)
    _git(meta, "init", "-q")
    bootstrap = CidxMetaBackupBootstrap()
    bootstrap._git(str(meta), "remote", "add", "origin", URL_WITH_USERINFO)

    with pytest.raises(Exception) as raised:
        bootstrap._git(str(meta), "remote", "add", "origin", URL_WITH_USERINFO)

    assert SECRET not in str(raised.value)
    assert SECRET not in repr(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_a_sync_push_failure_masks_the_remote_credential(tmp_path: Path) -> None:
    origin = _echoing_origin(tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "master")
    (seed / "a.md").write_text("a\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    (origin / "hooks" / "pre-receive").rename(origin / "hooks" / "held")
    _git(seed, "push", "-q", origin.as_uri(), "master")
    (origin / "hooks" / "held").rename(origin / "hooks" / "pre-receive")
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", origin.as_uri(), str(repo))
    (repo / "b.md").write_text("b\n")

    result = CidxMetaBackupSync(str(repo), "master").sync()

    assert result.sync_failure is not None
    assert "refused" in result.sync_failure
    assert SECRET not in result.sync_failure
