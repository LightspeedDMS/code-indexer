"""Bug #1832: RepositoryListingManager.get_available_branches's
"Failed to get branches for repository" diagnostic (line ~286) used only
`result.stderr` -- when the failing `git ls-remote` subprocess writes its
real diagnostic to stdout instead, the message degrades to an empty tail.

Discriminating case (AC5): stderr EMPTY, stdout NON-EMPTY. A test using a
non-empty stderr would pass before the fix and prove nothing.

Note (found while writing this test, not in the issue's line list): the
`RepositoryListingError` raised at line ~285 is immediately swallowed by
this method's own broad `except Exception:` (line ~307), which logs a
WARNING and returns the golden repo's default branch -- it never
propagates to the caller. That existing except handler ALSO builds its log
message via `format_error_log(..., "...{alias}: {e}")` with literal,
never-interpolated `{alias}`/`{e}` placeholders (format_error_log appends
**context as trailing `key=value` pairs; it does not `.format()` the
message string) -- so today the logged warning is always the literal text
"Failed to get branches for repository {alias}: {e}", carrying ZERO
information regardless of what failed. Both issues are fixed together
here (same file, same diagnostic-text-only scope, AC6 preserved: the
except-and-return-default control flow is unchanged) so the improved
diagnostic actually reaches the log instead of being silently discarded.
"""

from __future__ import annotations

import logging
import os
import tempfile
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
)
from code_indexer.server.repositories.repository_listing_manager import (
    RepositoryListingManager,
)

_DISCRIMINATING_STDOUT = "fatal: unable to access remote: connection reset by peer"


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def golden_repo_manager(temp_data_dir):
    manager = GoldenRepoManager(data_dir=temp_data_dir)
    clone_path = os.path.join(temp_data_dir, "golden-repos", "python-project")
    manager.golden_repos["python-project"] = GoldenRepo(
        alias="python-project",
        repo_url="https://github.com/user/python-project.git",
        default_branch="main",
        clone_path=clone_path,
        created_at="2024-01-01T00:00:00+00:00",
    )
    manager._sqlite_backend.add_repo(
        alias="python-project",
        repo_url="https://github.com/user/python-project.git",
        default_branch="main",
        clone_path=clone_path,
        created_at="2024-01-01T00:00:00+00:00",
        enable_temporal=False,
        temporal_options=None,
    )
    return manager


@pytest.fixture
def repository_listing_manager(temp_data_dir, golden_repo_manager):
    activated_repo_manager = ActivatedRepoManager(data_dir=temp_data_dir)
    return RepositoryListingManager(
        golden_repo_manager=golden_repo_manager,
        activated_repo_manager=activated_repo_manager,
    )


class TestGetAvailableBranchesDiagnostic:
    def test_empty_stderr_nonempty_stdout_surfaces_in_warning_log(
        self, repository_listing_manager, caplog
    ) -> None:
        with (
            patch(
                "code_indexer.server.repositories.repository_listing_manager"
                ".subprocess.run",
                return_value=Mock(
                    args=["git", "ls-remote", "--heads"],
                    returncode=1,
                    stdout=_DISCRIMINATING_STDOUT,
                    stderr="",
                ),
            ),
            caplog.at_level(logging.WARNING),
        ):
            # Control flow is unchanged (AC6): a failed git ls-remote falls
            # back to the golden repo's default branch, never raises.
            result = repository_listing_manager.get_available_branches(
                alias="python-project"
            )

        assert result == ["main"]

        warnings = [
            record.message
            for record in caplog.records
            if "Failed to get branches" in record.message
        ]
        assert warnings, f"expected a branches-failure warning, got: {caplog.records}"
        assert _DISCRIMINATING_STDOUT in warnings[0], (
            f"stdout diagnostic missing from warning: {warnings[0]!r}"
        )
