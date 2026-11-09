"""issue-64616: delegated X-Ray Phase 2 runs on the tree Phase 1 searched.

Phase 1 reads a golden repo through the content seam at
``golden_repo_content_root`` (a versioned snapshot -> its base clone). Phase 2
handed the delegated backend the raw ``.versioned/<repo>/v_N`` path, which
under a delegated IndexStore holds only ``.code-indexer``: the sandbox mounted
an empty tree and every scan reported "completed, 0 matches". Now both phases
read the base clone (scan and graph mode); pod-local repositories and the
local content default are unchanged.

Also: in-pod xray-cli output reporting ``files_errored`` is never "no
findings" -- all errored is a whole-batch XRayCliError, some is one
batch-level evaluation error carrying the count.
"""

from __future__ import annotations

import builtins
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from code_indexer.server.startup import content_backend_wiring as wiring
from cidx_xray.server.backend_wiring import (
    xray_execution_backend_for,
)
from cidx_xray.execution_backend import (
    XrayGraphRequest,
    XrayScanOutcome,
    XrayScanRequest,
)
from cidx_xray.rust_backend import RustNativeBackend

REPO = "evo-multi-idp-jwt-validator"


class _Delegated:
    is_local = False


class _Recording:
    """A delegated X-Ray backend that records what it was asked to read."""

    def __init__(self) -> None:
        self.scans: List[XrayScanRequest] = []
        self.graphs: List[XrayGraphRequest] = []

    def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome:
        self.scans.append(request)
        return XrayScanOutcome([([], [], None) for _ in request.file_specs])

    def execute_graph_analysis(self, request: XrayGraphRequest) -> Dict[str, Any]:
        self.graphs.append(request)
        return {"findings": []}


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """golden-repos/ with the base clone's registry row and an index-only
    versioned snapshot (what a pod holds under a delegated IndexStore)."""
    from code_indexer.server.repositories import golden_repo_manager as grm

    base = tmp_path / "golden-repos"
    (base / REPO).mkdir(parents=True)
    snapshot = base / ".versioned" / REPO / "v_1792515580"
    (snapshot / ".code-indexer").mkdir(parents=True)
    manager = SimpleNamespace(
        get_golden_repo=lambda name: (
            SimpleNamespace(clone_path=str(base / REPO), repo_url="https://x/r.git")
            if name == REPO
            else None
        )
    )
    monkeypatch.setattr(grm, "get_golden_repo_manager", lambda: manager)
    return base


def _forbid_pod_reads(monkeypatch: pytest.MonkeyPatch, fence: Path) -> None:
    """Any Python read or listing under golden-repos/ raises."""

    def guard(original: Any) -> Any:
        def guarded(target: Any, *a: Any, **kw: Any) -> Any:
            if str(target).startswith(str(fence)):
                raise AssertionError(f"pod read under delegation: {target}")
            return original(target, *a, **kw)

        return guarded

    for name in ("read_bytes", "read_text", "iterdir", "glob", "rglob", "is_file"):
        monkeypatch.setattr(Path, name, guard(getattr(Path, name)))
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    for name in ("listdir", "scandir", "walk"):
        monkeypatch.setattr(os, name, guard(getattr(os, name)))


def _snapshot(golden: Path) -> str:
    return str(golden / ".versioned" / REPO / "v_1792515580")


def test_scan_of_a_snapshot_alias_reads_the_base_clone(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wiring, "server_content_backend", lambda: _Delegated())
    recording = _Recording()
    backend = RustNativeBackend(xray_cache_backend=None, execution_backend=recording)
    _forbid_pod_reads(monkeypatch, golden)

    backend.run_batch(
        evaluator_code="e",
        file_specs=[{"file_path": "src/assert.ts", "lang": "typescript"}],
        repo_path=_snapshot(golden),
    )

    assert [r.repo_path for r in recording.scans] == [str(golden / REPO)]


def test_graph_of_a_snapshot_alias_reads_the_base_clone(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wiring, "server_content_backend", lambda: _Delegated())
    recording = _Recording()
    backend = RustNativeBackend(xray_cache_backend=None, execution_backend=recording)
    _forbid_pod_reads(monkeypatch, golden)

    backend.run_graph_analysis(
        evaluator_code="e", repo_root=_snapshot(golden), file_paths=["src/assert.ts"]
    )

    assert [r.repo_root for r in recording.graphs] == [str(golden / REPO)]
    assert recording.graphs[0].file_paths == ["src/assert.ts"]


def test_a_base_clone_path_is_passed_unchanged(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wiring, "server_content_backend", lambda: _Delegated())
    recording = _Recording()
    assert xray_execution_backend_for(recording, str(golden / REPO), []) is recording


def test_local_content_keeps_the_snapshot_path(golden: Path) -> None:
    recording = _Recording()
    assert xray_execution_backend_for(recording, _snapshot(golden), []) is recording


def test_a_pod_local_repository_stays_inline(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cidx_xray.inline_execution import PodLocalInlineXrayBackend

    monkeypatch.setattr(wiring, "server_content_backend", lambda: _Delegated())
    monkeypatch.setattr(wiring, "is_pod_local_golden_path", lambda path: True)
    backend = xray_execution_backend_for(_Recording(), _snapshot(golden), [])
    assert isinstance(backend, PodLocalInlineXrayBackend)


# ── files_errored ────────────────────────────────────────────────────────────


def _in_pod_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output: Dict[str, Any]
) -> RustNativeBackend:
    binary = tmp_path / "xray-cli"
    binary.write_text("")
    backend = RustNativeBackend(xray_cache_backend=None)
    monkeypatch.setattr(backend, "_xray_cli_path", binary)
    monkeypatch.setattr(backend, "_validate_rust_code", lambda code: (code, None))
    monkeypatch.setattr(
        backend, "_invoke_xray_cli", lambda *a, **k: (json.dumps(output), None)
    )
    return backend


def _specs(*names: str) -> List[Dict[str, Any]]:
    return [{"file_path": n, "lang": "python", "match_positions": []} for n in names]


def test_every_candidate_errored_is_a_whole_batch_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _in_pod_backend(
        monkeypatch,
        tmp_path,
        {"findings": [], "files_parsed": 0, "files_errored": 2, "cached": True},
    )

    results = backend.run_batch(
        evaluator_code="e", file_specs=_specs("a.py", "b.py"), repo_path=str(tmp_path)
    )

    assert len(results) == 1
    (error,) = results[0][1]
    assert error["file_path"] == ""
    assert error["error_type"] == "XRayCliError"
    assert (
        error["error_message"]
        == "xray-cli could not read/parse any of 2 candidate files"
    )


def test_an_unreadable_candidate_gets_its_own_error_and_one_result_per_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate that is not a readable file here is one xray-cli could not
    read: the error is on ITS tuple (batching-invariant), one tuple per spec."""
    (tmp_path / "b.py").write_text("x = 1\n")
    (tmp_path / "c.py").write_text("y = 2\n")
    backend = _in_pod_backend(
        monkeypatch,
        tmp_path,
        {"findings": [], "files_parsed": 2, "files_errored": 1, "cached": True},
    )

    results = backend.run_batch(
        evaluator_code="e",
        file_specs=_specs("b.py", "a.py", "c.py"),
        repo_path=str(tmp_path),
    )

    assert len(results) == 3
    assert results[0] == ([], [], None) and results[2] == ([], [], None)
    (error,) = results[1][1]
    assert error["file_path"] == "a.py"
    assert error["error_type"] == "XRayFilesErrored"
    assert (
        error["error_message"]
        == "xray-cli could not read/parse a.py: not a readable file"
    )


def test_an_unattributable_failure_is_one_batch_level_error_in_the_first_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every candidate is a readable file, yet xray-cli could not parse one:
    the count travels as a batch-level error (file_path == "") inside the
    first tuple, in the plugin's wording, still one tuple per spec."""
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("x = 1\n")
    backend = _in_pod_backend(
        monkeypatch,
        tmp_path,
        {"findings": [], "files_parsed": 2, "files_errored": 1, "cached": True},
    )

    results = backend.run_batch(
        evaluator_code="e",
        file_specs=_specs("a.py", "b.py", "c.py"),
        repo_path=str(tmp_path),
    )

    assert len(results) == 3
    (error,) = results[0][1]
    assert error["file_path"] == ""
    assert error["error_type"] == "XRayFilesErrored"
    assert (
        error["error_message"] == "xray-cli could not read/parse 1 of 3 candidate files"
    )
    assert results[1] == ([], [], None) and results[2] == ([], [], None)


def test_with_batch_error_keeps_the_first_tuples_matches_and_errors() -> None:
    from cidx_xray.rust_backend import with_batch_error

    match = {"file_path": "a.py", "line_number": 1}
    own = {"file_path": "a.py", "error_type": "E", "error_message": "m"}
    results = [([match], [own], {"m": 1}), ([], [], None)]

    out = with_batch_error(results, "XRayFilesErrored", "msg")

    assert len(out) == 2
    assert out[0][0] == [match] and out[0][2] == {"m": 1}
    assert out[0][1][0] == own and out[0][1][1]["file_path"] == ""
    assert out[1] == ([], [], None)
    assert with_batch_error([], "XRayFilesErrored", "msg") == []


def test_no_errored_files_add_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _in_pod_backend(
        monkeypatch,
        tmp_path,
        {"findings": [], "files_parsed": 1, "files_errored": 0, "cached": True},
    )
    results = backend.run_batch(
        evaluator_code="e", file_specs=_specs("a.py"), repo_path=str(tmp_path)
    )
    assert results == [([], [], None)]
