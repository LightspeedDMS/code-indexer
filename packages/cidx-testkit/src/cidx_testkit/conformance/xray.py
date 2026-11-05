"""Conformance suite for the X-Ray seam (``cidx_sdk.seams.xray``).

Every X-Ray execution backend -- the feature's in-pod engine, each delegate,
the testkit's sandbox-mcp fake -- must pass the same checks. Subclass
:class:`XrayBackendContract` and provide the ``xray_subject`` fixture: an
:class:`XraySubject` holding the backend, the repository root it scans, a way
to lay files out there, and how to spell an evaluator FOR THAT BACKEND that
reports every line containing a needle (real Rust for the in-pod engine,
:func:`cidx_testkit.fakes.sandbox_mcp.fake_evaluator` for the fake).

A backend that also offers inline execution
(:class:`~cidx_sdk.seams.xray.XrayInlineExecutionBackend`) is checked by
:class:`XrayInlineContract` as well: the same request over the same bytes,
sent inline, must answer exactly what the mounted run answers.

The checks are the seam's contract as the feature relies on it: never raise
for a known failure (error tuples; ``file_path == ""`` for a whole batch; a
graph answers ``{"ok": False, "error": {...}}``), one result tuple per file
spec in spec order, ``debug_messages`` None or a list of strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from cidx_sdk.seams.xray import (
    BatchResult,
    XrayBackend,
    XrayGraphRequest,
    XrayInlineExecutionBackend,
    XrayScanOutcome,
    XrayScanRequest,
)


@dataclass
class XraySubject:
    """A backend under test and the repository it scans.

    Attributes:
        backend: the implementation under test.
        repo_root: the repository root as the BACKEND is addressed with it.
        write: ``write(rel, data)`` creates a file (and its directories).
        evaluator_for: ``evaluator_for(needle)`` -> evaluator source that
            reports every line containing *needle*.
        invalid_evaluator: source the backend must refuse (validation).
        local_root: where the repository's files are on THIS machine (for
            inline execution, which sends them).
    """

    backend: XrayBackend
    repo_root: str
    write: Callable[[str, bytes], None]
    evaluator_for: Callable[[str], str]
    local_root: Path
    invalid_evaluator: str = "this is not an evaluator"


_FILES: Dict[str, bytes] = {
    "src/app.py": b"import os\nTOKEN = 'example_needle'\nprint(TOKEN)\n",
    "src/util.py": b"def helper():\n    return 1\n",
    "lib/other.py": b"# example_needle here\nx = 2\n# and example_needle again\n",
}
NEEDLE = "example_needle"
#: A graph is built over all its files in one run, so it stays small.
_GRAPH_FILES = ("lib/other.py", "src/app.py")


def _specs(paths: List[str]) -> List[Dict[str, Any]]:
    return [{"file_path": p, "lang": "python", "match_positions": []} for p in paths]


def _all_errors(results: BatchResult) -> bool:
    return bool(results) and all(
        errors and not matches for matches, errors, _ in results
    )


class XrayBackendContract:
    """The X-Ray seam's contract. Subclass and provide ``xray_subject``."""

    @pytest.fixture
    def xray_subject(self, tmp_path: Path) -> XraySubject:
        raise NotImplementedError("provide the backend under test")

    @pytest.fixture
    def populated(self, xray_subject: XraySubject) -> XraySubject:
        for rel, data in _FILES.items():
            xray_subject.write(rel, data)
        return xray_subject

    @staticmethod
    def scan(subject: XraySubject, paths: List[str], code: str) -> XrayScanOutcome:
        outcome = subject.backend.execute_scan(
            XrayScanRequest(
                evaluator_code=code,
                file_specs=_specs(paths),
                repo_path=subject.repo_root,
                timeout_seconds=60,
            )
        )
        assert isinstance(outcome, XrayScanOutcome), type(outcome)
        if outcome.debug_messages is not None:
            assert all(isinstance(m, str) for m in outcome.debug_messages)
        return outcome

    def test_satisfies_the_protocol(self, xray_subject: XraySubject) -> None:
        assert isinstance(xray_subject.backend, XrayBackend)

    def test_no_files_is_no_results(self, populated: XraySubject) -> None:
        assert self.scan(populated, [], populated.evaluator_for(NEEDLE)).results == []

    def test_one_result_per_spec_in_spec_order(self, populated: XraySubject) -> None:
        paths = ["lib/other.py", "src/util.py", "src/app.py"]
        results = self.scan(populated, paths, populated.evaluator_for(NEEDLE)).results
        assert len(results) == len(paths)
        lines = [sorted(m["line_number"] for m in matches) for matches, _, _ in results]
        assert lines == [[1, 3], [], [2]]
        for (matches, errors, _meta), path in zip(results, paths):
            assert errors == []
            assert all(m["file_path"] == path for m in matches)
        first_match = results[2][0][0]
        assert NEEDLE in first_match["line_content"]

    def test_an_invalid_evaluator_is_reported_not_raised(
        self, populated: XraySubject
    ) -> None:
        outcome = self.scan(populated, ["src/app.py"], populated.invalid_evaluator)
        assert _all_errors(outcome.results)

    def test_a_graph_answers_a_dict(self, populated: XraySubject) -> None:
        result = populated.backend.execute_graph_analysis(
            XrayGraphRequest(
                evaluator_code=populated.evaluator_for(NEEDLE),
                repo_root=populated.repo_root,
                file_paths=list(_GRAPH_FILES),
                timeout_seconds=60,
            )
        )
        assert isinstance(result, dict)
        assert set(result) >= {"ok", "error", "status", "findings"}
        assert result["ok"] is True, result["error"]
        assert result["error"] is None
        assert isinstance(result["findings"], list)

    def test_a_graph_with_an_invalid_evaluator_is_an_error_result(
        self, populated: XraySubject
    ) -> None:
        result = populated.backend.execute_graph_analysis(
            XrayGraphRequest(
                evaluator_code=populated.invalid_evaluator,
                repo_root=populated.repo_root,
                file_paths=list(_GRAPH_FILES),
                timeout_seconds=60,
            )
        )
        assert result["ok"] is False
        assert result["error"]["error_type"]


class XrayInlineContract(XrayBackendContract):
    """The inline extension's contract, on top of the scan contract."""

    def test_satisfies_the_inline_protocol(self, xray_subject: XraySubject) -> None:
        assert isinstance(xray_subject.backend, XrayInlineExecutionBackend)
        limits = xray_subject.backend.inline_limits
        assert limits.max_files > 0
        assert limits.max_file_bytes > 0
        assert limits.max_total_bytes > 0

    def test_inline_answers_what_the_mounted_run_answers(
        self, populated: XraySubject
    ) -> None:
        backend = populated.backend
        assert isinstance(backend, XrayInlineExecutionBackend)
        paths = ["src/app.py", "lib/other.py"]
        request = XrayScanRequest(
            evaluator_code=populated.evaluator_for(NEEDLE),
            file_specs=_specs(paths),
            repo_path=populated.repo_root,
            timeout_seconds=60,
        )
        files = {p: (populated.local_root / p).read_bytes() for p in paths}
        inline = backend.execute_scan_inline(request, files)
        mounted = backend.execute_scan(request)
        assert inline.results == mounted.results
