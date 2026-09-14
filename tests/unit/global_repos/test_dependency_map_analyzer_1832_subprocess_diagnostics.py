"""Bug #1832: DependencyMapAnalyzer._invoke_claude_cli's "Claude CLI
failed" error log (line ~2821) used only `result.stderr` -- when the
failing Claude CLI subprocess writes its real diagnostic to stdout
instead, the logged ERROR degrades to "Claude CLI failed: " with an empty
tail.

Discriminating case (AC5): stderr EMPTY, stdout NON-EMPTY. A test using a
non-empty stderr would pass before the fix and prove nothing.

Note: line ~2813 (a WARNING logged when stdout is EMPTY) already caps its
stderr display at 1000 chars -- that is the established precedent shape
Bug #1832 asks every site to match, and is untouched by this test/fix
(this test's discriminating case has non-empty stdout, a different branch).

Isolation fixture mirrors `_isolate_verification_semaphore` from
test_dependency_map_analyzer.py (Bug #1470): `_invoke_claude_cli` reaches
a process-wide semaphore singleton keyed by max_concurrent, so every test
that calls it directly must neutralize that singleton.
"""

from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer

_DISCRIMINATING_STDOUT = "Error: model overloaded, please retry the request"


@pytest.fixture(autouse=True)
def _isolate_verification_semaphore():
    with patch(
        "code_indexer.global_repos.dependency_map_analyzer._get_verification_semaphore",
        side_effect=lambda max_concurrent: threading.Semaphore(max_concurrent),
    ):
        yield


@pytest.fixture
def analyzer(tmp_path):
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=600,
    )


class TestInvokeClaudeCliDiagnostic:
    def test_empty_stderr_nonempty_stdout_surfaces_in_error_log(
        self, analyzer, caplog
    ) -> None:
        with (
            patch("subprocess.run") as mock_subprocess,
            caplog.at_level(logging.ERROR),
        ):
            mock_subprocess.return_value = MagicMock(
                returncode=1,
                stdout=_DISCRIMINATING_STDOUT,
                stderr="",
            )

            with pytest.raises(Exception):  # noqa: B017 -- CalledProcessError re-raise
                analyzer._invoke_claude_cli(
                    prompt="test prompt",
                    timeout=60,
                    max_turns=0,
                )

        errors = [
            record.message
            for record in caplog.records
            if "Claude CLI failed" in record.message
        ]
        assert errors, (
            f"expected a 'Claude CLI failed' error log, got: {caplog.records}"
        )
        assert _DISCRIMINATING_STDOUT in errors[0], (
            f"stdout diagnostic missing from error: {errors[0]!r}"
        )
