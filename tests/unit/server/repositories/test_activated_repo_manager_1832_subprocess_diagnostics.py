"""Bug #1832: ActivatedRepoManager._clone_with_copy_on_write's
"cidx fix-config failed (non-fatal)" warning (line ~3877) used only
`result.stderr` -- when the failing `cidx fix-config` subprocess writes its
real diagnostic to stdout instead, the logged warning degrades to
"cidx fix-config failed (non-fatal): " with an empty tail.

Discriminating case (AC5): stderr EMPTY, stdout NON-EMPTY. A test using a
non-empty stderr would pass before the fix and prove nothing.

Mirrors the mocking pattern already established in
test_activated_repo_manager_subprocess_env_sanitization_1325.py's
TestCloneWithCopyOnWriteFixConfigSanitizesPythonPath (fake clone_backend +
subprocess.run side_effect).
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

_DISCRIMINATING_STDOUT = (
    "cidx fix-config: config path resolution failed for embedder voyage-code-3"
)


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as tmp:
        m = ActivatedRepoManager(
            data_dir=tmp,
            golden_repo_manager=Mock(),
            background_job_manager=Mock(),
        )
        yield m


class TestCloneWithCopyOnWriteFixConfigDiagnostic:
    def test_empty_stderr_nonempty_stdout_surfaces_in_warning(
        self, manager, tmp_path, caplog
    ) -> None:
        source_path = tmp_path / "source-repo"
        source_path.mkdir()
        dest_path = tmp_path / "dest-repo"

        def _fake_create_clone(src, dst, **kwargs):
            os.makedirs(dst, exist_ok=True)
            os.makedirs(os.path.join(dst, ".code-indexer"), exist_ok=True)
            return dst

        manager._clone_backend = Mock()
        manager._clone_backend.create_clone_at_path.side_effect = _fake_create_clone

        def _run(cmd, **kwargs):
            if cmd == ["cidx", "fix-config", "--force"]:
                return Mock(
                    args=cmd, returncode=1, stdout=_DISCRIMINATING_STDOUT, stderr=""
                )
            # Every other subprocess call in this method (git rev-parse
            # --is-bare-repository, etc.) succeeds -- irrelevant to this
            # discriminating case.
            return Mock(args=cmd, returncode=0, stdout="", stderr="")

        with (
            patch(
                "code_indexer.server.repositories.activated_repo_manager"
                ".subprocess.run",
                side_effect=_run,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = manager._clone_with_copy_on_write(str(source_path), str(dest_path))

        assert result is True  # non-fatal: clone still reports success

        warnings = [
            record.message
            for record in caplog.records
            if "fix-config failed" in record.message
        ]
        assert warnings, f"expected a fix-config warning log, got: {caplog.records}"
        assert _DISCRIMINATING_STDOUT in warnings[0], (
            f"stdout diagnostic missing from warning: {warnings[0]!r}"
        )
