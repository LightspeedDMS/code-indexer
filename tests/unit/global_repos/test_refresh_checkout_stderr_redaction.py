"""A failed branch-reset `git checkout` during a refresh logs git's stderr,
which can echo a remote URL with credentials: it is redacted first.

A real refresh over a real origin and golden clone; the only stand-in is a
fake `git` (a real process first on PATH) that fails `checkout` with a
chosen stderr and hands every other subcommand to the real git.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from code_indexer.server.storage.sqlite_backends import (
    GoldenRepoMetadataSqliteBackend,
)
from tests.unit.global_repos.test_refresh_git_cancel_2012 import (
    install_fake_git,
    make_git_repo_scheduler,
    make_origin_and_clone,
)
from tests.unit.global_repos.test_refresh_scheduler_cancel_2012 import ALIAS

FAKE_SECRET = "s3cr3t-example-token-value"


def test_failed_checkout_stderr_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    origin, master = make_origin_and_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    metadata = GoldenRepoMetadataSqliteBackend(
        str(master.parent.parent / "cidx_server.db")
    )
    metadata.ensure_table_exists()
    metadata.add_repo(
        alias="example-repo",
        repo_url=str(origin),
        default_branch="develop",
        clone_path=str(master),
        created_at="2026-01-01T00:00:00+00:00",
    )
    install_fake_git(
        tmp_path,
        monkeypatch,
        tmp_path / "pids.json",
        fail_on="checkout",
        fail_stderr=(
            "fatal: unable to access "
            f"'https://example-user:{FAKE_SECRET}@example.com/repo.git/'\n"
        ),
    )

    with caplog.at_level(logging.ERROR):
        scheduler._execute_refresh(ALIAS)

    reset_errors = [
        r.getMessage() for r in caplog.records if "Failed to reset" in r.getMessage()
    ]
    assert reset_errors, "the failed checkout must still be logged"
    assert all(FAKE_SECRET not in m for m in reset_errors), reset_errors
