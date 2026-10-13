"""The X-Ray execution seam (``xray/execution_backend.py``).

RustNativeBackend keeps the signatures every caller uses and hands each call to
an ``XrayExecutionBackend`` as ONE request. These tests pin that the seam is
really the path taken: an injected or server-parked backend receives exactly
one call per scan job / batch cell / graph analysis, carrying the inputs the
caller passed, and its answer is what the caller gets back. Without one, the
instance executes locally -- today's code path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from code_indexer.xray.execution_backend import (
    XrayExecutionBackend,
    XrayGraphRequest,
    XrayScanOutcome,
    XrayScanRequest,
)
from code_indexer.xray.rust_backend import (
    LocalXrayExecutionBackend,
    RustNativeBackend,
)

_EVALUATOR = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }"

_WIRING = (
    "code_indexer.server.startup.xray_backend_wiring.server_xray_execution_backend"
)


class _RecordingBackend:
    """Answers every request with a fixed result and records it."""

    def __init__(self, debug_messages: Any = None) -> None:
        self.scans: List[XrayScanRequest] = []
        self.graphs: List[XrayGraphRequest] = []
        self._debug_messages = debug_messages

    def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome:
        self.scans.append(request)
        results = [
            (
                [
                    {
                        "line_number": 1,
                        "file_path": spec["file_path"],
                        "language": spec.get("lang", ""),
                        "pattern": "",
                        "snippet": "",
                        "line_content": "",
                    }
                ],
                [],
                None,
            )
            for spec in request.file_specs
        ]
        return XrayScanOutcome(results, debug_messages=self._debug_messages)

    def execute_graph_analysis(self, request: XrayGraphRequest) -> Dict[str, Any]:
        self.graphs.append(request)
        return {"ok": True, "error": None, "findings": [], "marker": "fake-graph"}


def test_both_backends_satisfy_the_protocol() -> None:
    assert isinstance(_RecordingBackend(), XrayExecutionBackend)
    assert isinstance(LocalXrayExecutionBackend(), XrayExecutionBackend)


def test_without_a_backend_the_instance_executes_locally() -> None:
    with patch(_WIRING, return_value=None):
        backend = RustNativeBackend()
    assert backend._execution_backend is backend


def test_server_parked_backend_is_used_when_none_is_injected() -> None:
    parked = _RecordingBackend()
    with patch(_WIRING, return_value=parked):
        backend = RustNativeBackend()
    assert backend._execution_backend is parked


def test_injected_backend_wins_over_the_server_one() -> None:
    injected, parked = _RecordingBackend(), _RecordingBackend()
    with patch(_WIRING, return_value=parked):
        backend = RustNativeBackend(execution_backend=injected)
    assert backend._execution_backend is injected


def test_run_batch_is_one_scan_request_carrying_the_callers_inputs() -> None:
    fake = _RecordingBackend(debug_messages=["dbg"])
    backend = RustNativeBackend(execution_backend=fake)
    specs = [
        {"file_path": "a.py", "source": "", "lang": "python", "match_positions": []},
        {"file_path": "b.py", "source": "", "lang": "python", "match_positions": []},
    ]

    def _spawned(_proc: Any) -> None:
        return None

    results = backend.run_batch(
        evaluator_code=_EVALUATOR,
        file_specs=specs,
        timeout_seconds=17,
        on_process_spawned=_spawned,
        repo_path="/repo",
    )

    assert fake.scans == [
        XrayScanRequest(
            evaluator_code=_EVALUATOR,
            file_specs=specs,
            repo_path="/repo",
            timeout_seconds=17,
            on_process_spawned=_spawned,
        )
    ]
    assert [m[0][0]["file_path"] for m in results] == ["a.py", "b.py"]
    assert backend._last_debug_messages == ["dbg"]


def test_no_debug_messages_keeps_the_previous_side_channel() -> None:
    """A failed scan (debug_messages=None) never clears what was last seen --
    exactly what the pre-seam run_batch did on its error returns."""
    backend = RustNativeBackend(execution_backend=_RecordingBackend())
    backend._last_debug_messages = ["earlier"]
    backend.run_batch(
        evaluator_code=_EVALUATOR,
        file_specs=[{"file_path": "a.py", "lang": "python"}],
    )
    assert backend._last_debug_messages == ["earlier"]


def test_run_graph_analysis_is_one_graph_request() -> None:
    fake = _RecordingBackend()
    backend = RustNativeBackend(execution_backend=fake)

    result = backend.run_graph_analysis(
        evaluator_code=_EVALUATOR,
        repo_root="/repo",
        file_paths=["/repo/A.java", "/repo/B.java"],
        timeout_seconds=33,
        refine=True,
    )

    assert fake.graphs == [
        XrayGraphRequest(
            evaluator_code=_EVALUATOR,
            repo_root="/repo",
            file_paths=["/repo/A.java", "/repo/B.java"],
            timeout_seconds=33,
            refine=True,
        )
    ]
    assert result["marker"] == "fake-graph"


def test_local_execution_goes_through_execute_scan_once(tmp_path: Path) -> None:
    """Under local, run_batch reaches LocalXrayExecutionBackend.execute_scan
    once and returns what it returns (here: the ValidationError answer, so no
    xray-cli or rustc is needed)."""
    with patch(_WIRING, return_value=None):
        backend = RustNativeBackend()
    specs = [{"file_path": "a.py", "lang": "python"}]
    with patch.object(
        LocalXrayExecutionBackend,
        "execute_scan",
        autospec=True,
        side_effect=LocalXrayExecutionBackend.execute_scan,
    ) as spy:
        results = backend.run_batch(
            evaluator_code="not rust at all",
            file_specs=specs,
            repo_path=str(tmp_path),
        )
    assert spy.call_count == 1
    assert results[0][1][0]["error_type"] == "ValidationError"


def _write_repo(root: Path, names: List[str]) -> Path:
    root.mkdir()
    for name in names:
        (root / name).write_text("x = 1\n")
    return root


def test_search_engine_job_is_exactly_one_scan_call(tmp_path: Path) -> None:
    from code_indexer.xray.search_engine import XRaySearchEngine

    repo = _write_repo(tmp_path / "repo", ["a.py", "b.py", "c.py"])
    fake = _RecordingBackend(debug_messages=["from-fake"])
    with patch(_WIRING, return_value=fake):
        engine = XRaySearchEngine()

    result = engine.run(
        repo_path=repo,
        driver_regex=r".*\.py$",
        evaluator_code=_EVALUATOR,
        search_target="filename",
        timeout_seconds=60,
    )

    assert len(fake.scans) == 1, "one execution per job, never per file"
    request = fake.scans[0]
    assert sorted(s["file_path"] for s in request.file_specs) == [
        "a.py",
        "b.py",
        "c.py",
    ]
    assert request.repo_path == str(repo)
    assert request.evaluator_code == _EVALUATOR
    assert sorted(m["file_path"] for m in result["matches"]) == [
        "a.py",
        "b.py",
        "c.py",
    ]
    assert result["debug_output"] == ["from-fake"]


def test_batch_job_is_exactly_one_scan_call_per_cell(tmp_path: Path) -> None:
    from code_indexer.server.mcp.handlers import xray_batch

    repos = [
        {"alias": "r1", "path": _write_repo(tmp_path / "r1", ["a.py", "b.py"])},
        {"alias": "r2", "path": _write_repo(tmp_path / "r2", ["c.py"])},
    ]
    scans = [
        {"driver_regex": r".*\.py$", "search_target": "filename"},
        {"driver_regex": r"^a\.py$", "search_target": "filename"},
    ]
    fake = _RecordingBackend()
    bjm = type("Bjm", (), {"jobs": {}})()

    with (
        patch(_WIRING, return_value=fake),
        patch.object(xray_batch, "_get_xray_cell_limiter", return_value=None),
        patch.object(
            xray_batch,
            "resolve_batch_evaluator",
            return_value=(_EVALUATOR, None),
        ),
    ):
        xray_batch._run_xray_batch_job(
            resolved_repos=repos,
            scans=scans,
            repo_errors=[],
            cidx_meta_path=tmp_path / "cidx-meta",
            max_results=None,
            timeout_seconds=60,
            job_id="",
            bjm=bjm,
            progress_callback=lambda *_a, **_k: None,
        )

    # r1 x scan0, r1 x scan1, r2 x scan0 -- r2 x scan1 has no candidate, so
    # nothing is executed for it.
    cells = [
        (r.repo_path, sorted(s["file_path"] for s in r.file_specs)) for r in fake.scans
    ]
    assert cells == [
        (str(repos[0]["path"]), ["a.py", "b.py"]),
        (str(repos[0]["path"]), ["a.py"]),
        (str(repos[1]["path"]), ["c.py"]),
    ]


@pytest.mark.asyncio
async def test_graph_handler_pipeline_is_exactly_one_graph_call(
    tmp_path: Path,
) -> None:
    from code_indexer.server.mcp.handlers import xray_graph

    graph_evaluator = (
        "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {\n"
        "    Vec::new()\n"
        "}\n"
        "fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) "
        "-> GraphResult {\n"
        "    GraphResult::default()\n"
        "}\n"
    )
    files = [str(tmp_path / "A.java"), str(tmp_path / "B.java")]
    fake = _RecordingBackend()
    with (
        patch(_WIRING, return_value=fake),
        patch.object(
            xray_graph,
            "_resolve_repo_and_files",
            return_value=(tmp_path, files, False, 0, 0, [], None),
        ),
        patch.object(xray_graph, "_get_xray_cell_limiter", return_value=None),
    ):
        await xray_graph._run_analyze_graph_pipeline(
            graph_evaluator, "repo-global", [], [], 45, refine=True
        )

    assert fake.graphs == [
        XrayGraphRequest(
            evaluator_code=graph_evaluator,
            repo_root=str(tmp_path),
            file_paths=files,
            timeout_seconds=45,
            refine=True,
        )
    ]
