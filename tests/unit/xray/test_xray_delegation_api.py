"""The public API a delegated X-Ray backend imports (``xray/delegation_api``).

A delegated execution hands back what xray-cli printed in a sandbox; cidx's
own code must shape it, or local and delegated results drift. These tests pin
that contract: the exported surface, that every export does what the in-pod
path does, that match shaping with an explicit line source never reads this
pod's disk, that the graph pipeline over an injected step executor equals the
subprocess path on the same recorded outputs, and that the ABI constant is the
Rust one.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import pathlib
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import patch

import pytest

from code_indexer.xray import delegation_api as api
from code_indexer.xray import rust_backend as rb

VALID_EVALUATOR = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"""

_ASSEMBLE_RS = (
    Path(__file__).resolve().parents[3]
    / "rust"
    / "xray-core"
    / "src"
    / "compiler"
    / "assemble.rs"
)

EXPECTED_SURFACE = {
    "BatchResult",
    "CacheIdentityInfo",
    "GRAPH_STEPS",
    "GraphStep",
    "GraphStepExecutor",
    "LineContentSource",
    "XRAY_CLI_STDERR_ERROR_LIMIT",
    "XRAY_EVALUATOR_ABI_VERSION",
    "XrayExecutionBackend",
    "XrayGraphRequest",
    "XrayScanOutcome",
    "XrayScanRequest",
    "build_matches",
    "build_scan_results",
    "error_all",
    "error_tuple",
    "graph_error_result",
    "parse_cache_identity_output",
    "parse_json_output",
    "repo_relative_file_id",
    "run_graph_pipeline",
    "sanitize_error_message",
    "scan_error_type",
    "validate_rust_code",
    "xray_cli_exit_message",
    "xray_cli_timeout_message",
}


# ── the surface ──────────────────────────────────────────────────────────────


def test_the_public_surface_is_exactly_the_contract() -> None:
    """Removing or renaming an export breaks the installed plugin: change
    this set only together with the plugin."""
    assert set(api.__all__) == EXPECTED_SURFACE
    for name in api.__all__:
        assert hasattr(api, name), name


def test_public_names_are_the_in_pod_implementations() -> None:
    """Aliases, not copies -- there is one implementation of each."""
    assert api.error_tuple is rb._error_tuple
    assert api.error_all is rb._error_all
    assert api.graph_error_result is rb._graph_error_result
    assert api.sanitize_error_message is rb._sanitize_error_message
    assert api.repo_relative_file_id is rb._repo_relative_file_id
    assert api.XRAY_CLI_STDERR_ERROR_LIMIT == rb._XRAY_CLI_STDERR_ERROR_LIMIT


# ── result shaping ───────────────────────────────────────────────────────────


def test_error_tuple_and_error_all() -> None:
    assert api.error_tuple("", "SandboxError", "boom") == (
        [],
        [
            {
                "file_path": "",
                "line_number": 0,
                "error_type": "SandboxError",
                "error_message": "boom",
            }
        ],
        None,
    )
    specs = [{"file_path": "a.py"}, {"file_path": "b.py"}]
    assert api.error_all(specs, "ValidationError", "bad") == [
        api.error_tuple("a.py", "ValidationError", "bad"),
        api.error_tuple("b.py", "ValidationError", "bad"),
    ]


def test_graph_error_result_is_sanitized_and_honest() -> None:
    result = api.graph_error_result(
        "XRayCliError", "failed at /home/svc/repo/x.rs", build_status="partial"
    )
    assert result["ok"] is False
    assert result["error"] == {
        "error_type": "XRayCliError",
        "error_message": "failed at <server-path>",
    }
    assert result["build_status"] == "partial"
    assert result["findings"] == [] and result["candidate_count"] is None


def test_sanitize_error_message() -> None:
    msg = "error in /data/xray-cache/build-1-2/" + "a" * 64 + ".rs and /tmp/x/y"
    assert api.sanitize_error_message(msg) == "error in evaluator.rs and <server-path>"


def test_repo_relative_file_id_is_rusts_file_id() -> None:
    """identity::file_id: the first 4 bytes of sha256(path), big-endian."""
    for path in ("src/a.py", "Main.java", "dir with space/ü.rs"):
        digest = hashlib.sha256(path.encode("utf-8")).digest()
        assert api.repo_relative_file_id(path) == int.from_bytes(digest[:4], "big")


def test_validate_rust_code() -> None:
    assert api.validate_rust_code(VALID_EVALUATOR) == (VALID_EVALUATOR, None)
    code, error = api.validate_rust_code("fn nope() {}")
    assert code == "" and error


def test_parse_json_output() -> None:
    assert api.parse_json_output('{"findings": []}') == ({"findings": []}, None)
    output, error = api.parse_json_output("not json")
    assert output == {}
    assert error is not None and error.startswith("xray-cli produced non-JSON output")


def test_parse_cache_identity_output() -> None:
    info = api.parse_cache_identity_output(
        "identity=abc\nsource_hash=def\nabi_version=14\nrustc_version=rustc 1.91.0\n"
    )
    assert info == api.CacheIdentityInfo("abc", "def", 14, "rustc 1.91.0")
    assert api.parse_cache_identity_output("identity=abc\n") is None


@pytest.mark.parametrize(
    "output, expected",
    [
        ({"error": "e", "error_kind": "compile"}, "CompileError"),
        ({"error": "e", "error_kind": "infrastructure"}, "XRayCliError"),
        ({"error": "e"}, "XRayCliError"),
    ],
)
def test_scan_error_type(output: Dict[str, Any], expected: str) -> None:
    assert api.scan_error_type(output) == expected


def test_execute_scan_types_cli_errors_with_scan_error_type() -> None:
    """The in-pod scan uses the same mapping (it is not a second copy)."""
    backend = rb.LocalXrayExecutionBackend(xray_cache_backend=None)
    stdout = json.dumps({"error": "rustc failed", "error_kind": "compile"})
    with (
        patch.object(rb.LocalXrayExecutionBackend, "_invoke_xray_cli") as invoke,
        patch.object(rb, "scan_error_type", return_value="Sentinel") as mapping,
        patch.object(Path, "exists", return_value=True),
    ):
        invoke.return_value = (stdout, None)
        outcome = backend.execute_scan(
            api.XrayScanRequest(
                evaluator_code=VALID_EVALUATOR,
                file_specs=[{"file_path": "a.py"}],
                repo_path="/repo",
                timeout_seconds=5,
            )
        )
    mapping.assert_called_once()
    assert outcome.results[0][1][0]["error_type"] == "Sentinel"


def test_step_error_messages_are_the_subprocess_wording() -> None:
    assert api.xray_cli_timeout_message(7) == "xray-cli timed out after 7s"
    stderr = "x" * (api.XRAY_CLI_STDERR_ERROR_LIMIT + 50)
    assert api.xray_cli_exit_message(-9, stderr) == (
        "xray-cli exited with code -9: " + "x" * api.XRAY_CLI_STDERR_ERROR_LIMIT
    )


def test_run_xray_cli_process_words_failures_with_the_shared_formatters(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fail.sh"
    script.write_text("#!/bin/sh\necho oops >&2\nexit 3\n")
    script.chmod(0o755)
    backend = rb.LocalXrayExecutionBackend(xray_cache_backend=None)
    _stdout, error = backend._run_xray_cli_process(
        [str(script)], 10, None, acquire_compile_slot=False
    )
    assert error == api.sanitize_error_message(api.xray_cli_exit_message(3, "oops\n"))


# ── match shaping without a disk read ────────────────────────────────────────


@pytest.fixture
def no_disk_reads() -> Iterator[None]:
    def _refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("delegated match shaping must not read the disk")

    with (
        patch.object(pathlib.Path, "read_bytes", _refuse),
        patch.object(pathlib.Path, "read_text", _refuse),
        patch.object(builtins, "open", _refuse),
    ):
        yield


def test_build_matches_takes_line_content_only_from_its_source(
    tmp_path: Path, no_disk_reads: None
) -> None:
    on_disk = tmp_path / "a.py"
    spec = {"file_path": str(on_disk), "lang": "python"}
    findings: List[Dict[str, Any]] = [
        {"line": 2, "pattern": "p", "snippet": "s"},
        {"line": 9},
        {"line": 1, "line_content": "emitted by xray-cli"},
    ]
    lines = {2: "two", 1: "one"}
    matches = api.build_matches(
        spec, findings, line_content=lambda f: lines.get(f.get("line", 0), "")
    )
    assert matches == [
        {
            "line_number": 2,
            "file_path": str(on_disk),
            "language": "python",
            "pattern": "p",
            "snippet": "s",
            "line_content": "two",
        },
        {
            "line_number": 9,
            "file_path": str(on_disk),
            "language": "python",
            "pattern": "",
            "snippet": "",
            "line_content": "",
        },
        {
            "line_number": 1,
            "file_path": str(on_disk),
            "language": "python",
            "pattern": "",
            "snippet": "",
            "line_content": "one",
        },
    ]


def test_build_scan_results_groups_by_reported_path(no_disk_reads: None) -> None:
    specs = [
        {"file_path": "a.py", "lang": "python"},
        {"file_path": "b.py", "lang": "python"},
        {"file_path": "c.py", "lang": "python"},
    ]
    paths = ["/workspace/a.py", "/workspace/b.py", "/workspace/c.py"]
    findings = [
        {"file": "/workspace/c.py", "line": 3},
        {"file": "/workspace/a.py", "line": 1},
        {"file": "/elsewhere/a.py", "line": 7},
        {"file": "/workspace/a.py", "line": 2},
    ]
    asked: List[Tuple[str, str]] = []

    def source_for(spec: Dict[str, Any], path: str) -> api.LineContentSource:
        asked.append((spec["file_path"], path))
        return lambda f: f"{spec['file_path']}:{f['line']}"

    results = api.build_scan_results(
        specs, paths, findings, line_content_for=source_for
    )
    assert [[m["line_content"] for m in r[0]] for r in results] == [
        ["a.py:1", "a.py:2"],
        [],
        ["c.py:3"],
    ]
    assert results[1] == ([], [], None)
    assert all(r[1] == [] and r[2] is None for r in results)
    assert asked == [("a.py", "/workspace/a.py"), ("c.py", "/workspace/c.py")]


def test_build_scan_results_equals_the_in_pod_shaping(tmp_path: Path) -> None:
    """Same findings, same lines: the delegated entry point and the in-pod
    one produce identical results (one shaping implementation)."""
    (tmp_path / "a.py").write_bytes(b"x = 1\r\ny\xff = 2\n\nlast")
    (tmp_path / "b.py").write_text("only\n")
    specs = [{"file_path": "a.py", "lang": "python"}, {"file_path": "b.py"}]
    abs_paths = [str(tmp_path / s["file_path"]) for s in specs]
    findings = [
        {"file": abs_paths[0], "line": 1, "pattern": "p"},
        {"file": abs_paths[0], "line": 2},
        {"file": abs_paths[0], "line": 99},
        {"file": abs_paths[1], "line": 1, "snippet": "s"},
    ]
    backend = rb.LocalXrayExecutionBackend(xray_cache_backend=None)
    in_pod = backend._build_results(specs, abs_paths, findings, tmp_path.resolve())

    def source_for(_spec: Dict[str, Any], path: str) -> api.LineContentSource:
        lines = Path(path).read_bytes().decode("utf-8", errors="replace").splitlines()
        return lambda f: lines[f["line"] - 1] if 0 < f["line"] <= len(lines) else ""

    delegated = api.build_scan_results(
        specs, abs_paths, findings, line_content_for=source_for
    )
    assert delegated == in_pod


# ── graph pipeline over an injected step executor ────────────────────────────

_SO = "/cache/evaluator.so"
_FILES = ["a.py", "b/c.py", "d.py"]
_STEP_FLAGS = {
    "--compile-only": "compile",
    "--build-graph": "build",
    "--analyze-graph": "analyze",
    "--refine": "refine",
}


def _symbol(path: str, index: int = 0) -> int:
    return (api.repo_relative_file_id(path) << 32) | index


def _ok(payload: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    return json.dumps(payload), None


_COMPILE = _ok({"so_path": _SO, "cached": False, "compile_ms": 12})
_BUILD = _ok(
    {
        "status": "ok",
        "result": {
            "fact_graph_complete": True,
            "files_with_parse_errors": 0,
            "completeness_reasons": [],
            "candidate_count": 3,
            "candidate_budget_limit": 100,
        },
    }
)


def _analyze(refine: List[int]) -> Tuple[str, Optional[str]]:
    return _ok(
        {"status": "ran_ok", "result": {"findings": [{"n": 1}], "refine": refine}}
    )


_FAIL = ("", "xray-cli exited with code 2: boom")

# name -> (recorded step outputs, refine, remaining budget before each step)
SCENARIOS: Dict[str, Tuple[Dict[str, Tuple[str, Optional[str]]], bool, List[float]]] = {
    "refine_ran": (
        {
            "compile": _COMPILE,
            "build": _BUILD,
            "analyze": _analyze([_symbol("d.py"), _symbol("a.py", 2), _symbol("zz")]),
            "refine": _ok(
                {"status": "ran_ok", "result": {"files": [{"findings": [{"r": 1}]}]}}
            ),
        },
        True,
        [60.0, 50.5, 40.2, 30.9],
    ),
    "refine_absent": (
        {
            "compile": _COMPILE,
            "build": _BUILD,
            "analyze": _analyze([_symbol("a.py")]),
            "refine": _ok({"status": "absent"}),
        },
        True,
        [60.0, 50.0, 40.0, 30.0],
    ),
    "refine_failed": (
        {
            "compile": _COMPILE,
            "build": _BUILD,
            "analyze": _analyze([_symbol("a.py")]),
            "refine": _FAIL,
        },
        True,
        [60.0, 50.0, 40.0, 30.0],
    ),
    "refine_deadline": (
        {"compile": _COMPILE, "build": _BUILD, "analyze": _analyze([_symbol("a.py")])},
        True,
        [60.0, 50.0, 40.0, 0.0],
    ),
    "not_requested": (
        {"compile": _COMPILE, "build": _BUILD, "analyze": _analyze([_symbol("a.py")])},
        False,
        [60.0, 50.0, 40.0],
    ),
    "compile_error": (
        {"compile": _ok({"error": "error[E0308]: mismatched types"})},
        False,
        [60.0],
    ),
    "compile_deadline": ({}, False, [0.0]),
    "build_failed": (
        {"compile": _COMPILE, "build": _ok({"status": "partial"})},
        False,
        [60.0, 50.0],
    ),
    "analyze_deadline": (
        {"compile": _COMPILE, "build": _BUILD},
        False,
        [60.0, 50.0, -1.0],
    ),
    "analyze_failed": (
        {"compile": _COMPILE, "build": _BUILD, "analyze": _FAIL},
        False,
        [60.0, 50.0, 40.0],
    ),
    "analyze_malformed": (
        {"compile": _COMPILE, "build": _BUILD, "analyze": _ok({"status": "ran_ok"})},
        False,
        [60.0, 50.0, 40.0],
    ),
}


class _ReplayExecutor(api.GraphStepExecutor):
    """What a delegated backend does: answer each step from a recording."""

    def __init__(
        self, outputs: Dict[str, Tuple[str, Optional[str]]], remaining: List[float]
    ) -> None:
        self._outputs = outputs
        self._remaining = dict(zip(api.GRAPH_STEPS, remaining))
        self.asked: List[str] = []
        self.ran: List[api.GraphStep] = []

    def remaining_seconds(self, step: str) -> float:
        self.asked.append(step)
        return self._remaining[step]

    def run_step(self, step: api.GraphStep) -> Tuple[str, Optional[str]]:
        self.ran.append(step)
        return self._outputs[step.name]


def _run_in_pod(
    outputs: Dict[str, Tuple[str, Optional[str]]],
    refine: bool,
    remaining: List[float],
) -> Tuple[Dict[str, Any], List[Tuple[str, int, Optional[str]]]]:
    """The subprocess path, with xray-cli answering from the same recording."""
    budget = iter(remaining)
    spawned: List[Tuple[str, int, Optional[str]]] = []

    def fake_process(
        self: Any,
        cmd: List[str],
        timeout_seconds: int,
        on_process_spawned: Any,
        acquire_compile_slot: bool,
        apply_compile_limits: bool = False,
    ) -> Tuple[str, Optional[str]]:
        name = _STEP_FLAGS[cmd[1]]
        files = None
        if name == "refine":
            files = Path(cmd[cmd.index("--files-from") + 1]).read_text()
        spawned.append((name, timeout_seconds, files))
        return outputs[name]

    backend = rb.LocalXrayExecutionBackend(xray_cache_backend=None)
    with (
        patch.object(
            rb.LocalXrayExecutionBackend, "_run_xray_cli_process", fake_process
        ),
        patch.object(
            rb.LocalXrayExecutionBackend,
            "_remaining_seconds",
            staticmethod(lambda _deadline: next(budget)),
        ),
    ):
        result = backend._compile_build_and_analyze(
            VALID_EVALUATOR,
            "/repo",
            "/tmp/eval.rs",
            "/tmp/files.txt",
            Path("/tmp/graph.bin"),
            Path("/tmp/facts.json"),
            0.0,
            refine=refine,
            file_paths=_FILES,
        )
    return result, spawned


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_injected_executor_equals_the_subprocess_path(scenario: str) -> None:
    outputs, refine, remaining = SCENARIOS[scenario]
    in_pod, spawned = _run_in_pod(outputs, refine, remaining)
    replay = _ReplayExecutor(outputs, remaining)
    delegated = api.run_graph_pipeline(replay, refine=refine, file_paths=_FILES)

    assert delegated == in_pod
    # The same steps ran, with the same budgets and the same refine set.
    assert [(s.name, s.timeout_seconds) for s in replay.ran] == [
        (name, timeout) for name, timeout, _ in spawned
    ]
    for step, (_name, _timeout, files) in zip(replay.ran, spawned):
        if step.name == "refine":
            assert files == "\n".join(step.refine_files or [])
        if step.name != "compile":
            assert step.so_path == _SO
    # The budget is asked once per step, in pipeline order.
    assert replay.asked == list(api.GRAPH_STEPS[: len(replay.asked)])


def test_refine_runs_only_over_the_narrowed_files() -> None:
    outputs, _refine, remaining = SCENARIOS["refine_ran"]
    replay = _ReplayExecutor(outputs, remaining)
    result = api.run_graph_pipeline(replay, refine=True, file_paths=_FILES)
    assert result["refine_status"] == "ran"
    assert replay.ran[-1].refine_files == ["a.py", "d.py"]


def test_executor_hooks_wrap_a_successful_compile() -> None:
    events: List[Any] = []

    class _Hooked(_ReplayExecutor):
        def before_compile(self, deadline_seconds: Optional[float]) -> None:
            events.append(("before", deadline_seconds))

        def after_compile(
            self, compile_info: Dict[str, Any], deadline_seconds: Optional[float]
        ) -> None:
            events.append(("after", compile_info, deadline_seconds))

    outputs, _refine, remaining = SCENARIOS["not_requested"]
    api.run_graph_pipeline(_Hooked(outputs, remaining), file_paths=_FILES)
    assert events == [
        ("before", 60.0),
        ("after", {"cached": False, "compile_ms": 12}, 60.0),
    ]


def test_graph_step_executor_requires_both_primitives() -> None:
    class _Partial(api.GraphStepExecutor):
        def remaining_seconds(self, step: str) -> float:
            return 1.0

    partial: Any = _Partial
    with pytest.raises(TypeError):
        partial()


# ── the evaluator ABI ────────────────────────────────────────────────────────


def _rust_abi_version() -> int:
    match = re.search(
        r"pub const XRAY_ABI_VERSION: u64 = (\d+);", _ASSEMBLE_RS.read_text()
    )
    assert match, f"XRAY_ABI_VERSION not found in {_ASSEMBLE_RS}"
    return int(match.group(1))


def test_abi_constant_is_the_rust_source_of_truth() -> None:
    """Bump XRAY_EVALUATOR_ABI_VERSION with assemble.rs's XRAY_ABI_VERSION."""
    assert api.XRAY_EVALUATOR_ABI_VERSION == _rust_abi_version()


def test_abi_constant_matches_the_shipped_xray_cli() -> None:
    from tests.unit.xray.conftest import require_xray_cli_binary

    require_xray_cli_binary()
    done = subprocess.run(
        [str(rb.resolve_xray_cli_path()), "--print-cache-identity"],
        input=VALID_EVALUATOR,
        capture_output=True,
        text=True,
        timeout=30,
        env=rb.xray_subprocess_env(),
    )
    info = api.parse_cache_identity_output(done.stdout)
    assert done.returncode == 0 and info is not None, done.stderr
    assert info.abi_version == api.XRAY_EVALUATOR_ABI_VERSION
