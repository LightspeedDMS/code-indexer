"""X-Ray over a pod-local repository: read here, executed inline in the sandbox.

``PodLocalInlineXrayBackend`` (xray/inline_execution.py) is what
``xray_execution_backend_for`` returns for a pod-local repository under
``xray_backend=delegated``. It reads the candidate files on this pod, batches
them under the plugin's ``inline_limits`` and sends their bytes through the
plugin's ``execute_*_inline``. These tests pin:

* parity -- a batched run returns exactly what ONE run over the repository
  returns (results in spec order, debug lines in order), for any limits, and
  with the real xray-cli, exactly what the local backend returns;
* fail loud -- every refusal (a file over the per-file limit, a graph over any
  limit, a symlink out of the repository, an unreadable file, a plugin without
  inline support, a sandbox error in any batch, a deadline spent between
  batches) is a whole-batch error, sent before nothing is truncated;
* the local default (``xray_backend=local``) is untouched: same request to the
  in-pod backend, the inline module never involved.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from code_indexer.server.startup import content_backend_wiring as content_wiring
from code_indexer.server.startup import xray_backend_wiring as xray_wiring
from code_indexer.xray import delegation_api as api
from code_indexer.xray import inline_execution as inline
from code_indexer.xray.execution_backend import (
    XrayGraphRequest,
    XrayScanOutcome,
    XrayScanRequest,
)
from code_indexer.xray.inline_execution import (
    INLINE_LIMIT_EXCEEDED,
    INLINE_READ_REFUSED,
    INLINE_UNSUPPORTED,
    InlineExecutionError,
    InlineLimits,
    PodLocalInlineXrayBackend,
    plan_batches,
    read_candidates,
)
from code_indexer.xray.rust_backend import LocalXrayExecutionBackend, RustNativeBackend

_EVALUATOR = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }"
WIDE = InlineLimits(max_files=10000, max_file_bytes=1 << 22, max_total_bytes=12 << 20)

# A per-file "scan" whose answer depends only on (path, bytes) -- as X-Ray's
# does: every file is evaluated on its own.
ScanFn = Callable[[XrayScanRequest, Path], XrayScanOutcome]


def _fake_scan(request: XrayScanRequest, root: Path) -> XrayScanOutcome:
    results: api.BatchResult = []
    debug: List[str] = []
    for spec in request.file_specs:
        rel = spec["file_path"]
        path = root / rel
        if not path.is_file():
            results.append(([], [{"file_path": rel, "error_type": "Missing"}], None))
            continue
        data = path.read_bytes()
        results.append(
            (
                [
                    {
                        "file_path": rel,
                        "line_number": len(data),
                        "snippet": data[:8].hex(),
                    }
                ],
                [],
                None,
            )
        )
        debug.append(f"saw {rel}")
    return XrayScanOutcome(results, debug_messages=debug)


class _Sandbox:
    """The plugin, with a sandbox that materialises the inline files (only
    those) in a fresh directory and runs *scan* there -- the runner's model."""

    def __init__(self, limits: InlineLimits = WIDE, scan: ScanFn = _fake_scan) -> None:
        self.inline_limits = limits
        self._scan = scan
        self.calls: List[Tuple[XrayScanRequest, Dict[str, bytes]]] = []
        self.graph_calls: List[Tuple[XrayGraphRequest, Dict[str, bytes]]] = []
        self.fail_on_call: Optional[int] = None
        self.answer_short_on_call: Optional[int] = None

    def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome:
        raise AssertionError("a pod-local repository went the mounted way")

    def execute_graph_analysis(self, request: XrayGraphRequest) -> Dict[str, Any]:
        raise AssertionError("a pod-local repository went the mounted way")

    def execute_scan_inline(
        self, request: XrayScanRequest, files: Dict[str, bytes]
    ) -> XrayScanOutcome:
        self.calls.append((request, dict(files)))
        assert len(files) <= self.inline_limits.max_files
        assert (
            len({s["file_path"] for s in request.file_specs})
            <= self.inline_limits.max_files
        )
        assert sum(map(len, files.values())) <= self.inline_limits.max_total_bytes
        if self.fail_on_call == len(self.calls):
            return XrayScanOutcome(
                [api.error_tuple("", "SandboxError", "sidecar died")]
            )
        work = Path(tempfile.mkdtemp(prefix="inline-sandbox-"))
        try:
            for rel, data in files.items():
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                (work / rel).write_bytes(data)
            outcome = self._scan(
                dataclasses.replace(request, repo_path=str(work)), work
            )
        finally:
            shutil.rmtree(work)
        if self.answer_short_on_call == len(self.calls):
            return XrayScanOutcome(outcome.results[:-1], outcome.debug_messages)
        return outcome

    def execute_graph_analysis_inline(
        self, request: XrayGraphRequest, files: Dict[str, bytes]
    ) -> Dict[str, Any]:
        self.graph_calls.append((request, dict(files)))
        return {"ok": True, "files": sorted(files)}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "golden-repos" / "langfuse_proj_alice"
    (root / "traces" / "deep").mkdir(parents=True)
    contents = {
        "a.py": b"x = 1\n",
        "traces/b.json": b'{"k": "v"}\n' * 3,
        "traces/deep/c.py": b"\xff\xfe not utf-8\n",
        "d.txt": b"",
        "e.py": b"def f():\n    if x:\n        if y:\n            pass\n",
        "f.md": "caf\u00e9 \u20ac\n".encode(),
    }
    for rel, data in contents.items():
        (root / rel).write_bytes(data)
    return root


SPECS_ORDER = [
    "e.py",
    "a.py",
    "missing.py",
    "traces/b.json",
    "d.txt",
    "traces/deep/c.py",
    "a.py",
    "f.md",
]


def _specs(rels: List[str]) -> List[Dict[str, Any]]:
    return [{"file_path": r, "lang": "python", "match_positions": []} for r in rels]


def _scan_req(repo: Path, rels: List[str], timeout: int = 60) -> XrayScanRequest:
    return XrayScanRequest(_EVALUATOR, _specs(rels), str(repo), timeout)


# --------------------------------------------------------------------------
# parity: batched == one run over the repository
# --------------------------------------------------------------------------


def test_one_batch_equals_a_run_over_the_repository(repo: Path) -> None:
    request = _scan_req(repo, SPECS_ORDER)
    sandbox = _Sandbox()
    inline_outcome = PodLocalInlineXrayBackend(sandbox).execute_scan(request)
    assert inline_outcome == _fake_scan(request, repo)
    assert len(sandbox.calls) == 1
    # Exactly the regular files were sent, a duplicate once, the missing one not.
    assert sorted(sandbox.calls[0][1]) == sorted(set(SPECS_ORDER) - {"missing.py"})


@pytest.mark.parametrize(
    "limits",
    [
        InlineLimits(max_files=1, max_file_bytes=64, max_total_bytes=1 << 20),
        InlineLimits(max_files=2, max_file_bytes=64, max_total_bytes=1 << 20),
        InlineLimits(max_files=3, max_file_bytes=50, max_total_bytes=55),
        InlineLimits(max_files=100, max_file_bytes=50, max_total_bytes=50),
        InlineLimits(max_files=100, max_file_bytes=64, max_total_bytes=64),
        InlineLimits(max_files=5, max_file_bytes=64, max_total_bytes=70),
    ],
)
def test_any_batching_merges_to_exactly_the_one_run_result(
    repo: Path, limits: InlineLimits
) -> None:
    request = _scan_req(repo, SPECS_ORDER)
    sandbox = _Sandbox(limits)
    batched = PodLocalInlineXrayBackend(sandbox).execute_scan(request)
    one_run = _fake_scan(request, repo)
    assert batched.results == one_run.results
    # Debug lines: the same lines. xray-cli itself gathers them in rayon
    # completion order (scanner.rs scan_files_parallel), so not even one run
    # fixes their order.
    assert batched.debug_messages is not None and one_run.debug_messages is not None
    assert sorted(batched.debug_messages) == sorted(one_run.debug_messages)
    assert len(sandbox.calls) > 1
    # Every spec exactly once; both "a.py" occurrences in one run.
    sent = [s["file_path"] for req, _ in sandbox.calls for s in req.file_specs]
    assert sorted(sent) == sorted(SPECS_ORDER)
    runs_with_a = [
        req
        for req, _ in sandbox.calls
        if any(s["file_path"] == "a.py" for s in req.file_specs)
    ]
    assert len(runs_with_a) == 1


def test_no_candidates_is_an_empty_result_without_a_call(repo: Path) -> None:
    sandbox = _Sandbox()
    assert PodLocalInlineXrayBackend(sandbox).execute_scan(
        _scan_req(repo, [])
    ) == XrayScanOutcome([])
    assert sandbox.calls == []


def test_every_batch_carries_the_callers_request_but_its_own_specs(repo: Path) -> None:
    sandbox = _Sandbox(
        InlineLimits(max_files=2, max_file_bytes=64, max_total_bytes=1 << 20)
    )
    calls: List[Any] = []
    request = dataclasses.replace(
        _scan_req(repo, SPECS_ORDER), on_process_spawned=calls.append
    )
    PodLocalInlineXrayBackend(sandbox).execute_scan(request)
    for sub, _files in sandbox.calls:
        assert sub.evaluator_code == _EVALUATOR
        assert sub.repo_path == str(repo)
        assert sub.on_process_spawned is request.on_process_spawned


# --------------------------------------------------------------------------
# plan_batches
# --------------------------------------------------------------------------


def test_batches_fill_to_the_byte_limit_exactly_and_no_further() -> None:
    files = {"a": b"12", "b": b"34", "c": b"5"}
    limits = InlineLimits(max_files=10, max_file_bytes=4, max_total_bytes=4)
    assert plan_batches(["a", "b", "c"], files, limits) == [[0, 1], [2]]
    limits5 = dataclasses.replace(limits, max_total_bytes=5)
    assert plan_batches(["a", "b", "c"], files, limits5) == [[0, 1, 2]]


def test_batches_fill_to_the_count_limit_exactly() -> None:
    limits = InlineLimits(max_files=2, max_file_bytes=4, max_total_bytes=100)
    assert plan_batches(["a", "b", "c", "d", "e"], {}, limits) == [[0, 1], [2, 3], [4]]


def test_absent_files_weigh_nothing_and_every_repeat_travels_with_the_first() -> None:
    files = {"a": b"1234"}
    limits = InlineLimits(max_files=10, max_file_bytes=4, max_total_bytes=4)
    assert plan_batches(["a", "x", "a", "y"], files, limits) == [[0, 1, 2, 3]]
    # Two paths per run: "a" (both occurrences) with "x", then "y".
    limits2 = InlineLimits(max_files=2, max_file_bytes=4, max_total_bytes=4)
    assert plan_batches(["a", "x", "y", "a"], files, limits2) == [[0, 1, 3], [2]]


# --------------------------------------------------------------------------
# fail loud
# --------------------------------------------------------------------------


def _whole_batch_error(outcome: XrayScanOutcome) -> Dict[str, Any]:
    assert inline.is_whole_batch_error(outcome.results), outcome.results
    return outcome.results[0][1][0]


def test_a_file_over_the_per_file_limit_refuses_the_request_before_any_call(
    repo: Path,
) -> None:
    sandbox = _Sandbox(
        InlineLimits(max_files=10, max_file_bytes=10, max_total_bytes=100)
    )
    err = _whole_batch_error(
        PodLocalInlineXrayBackend(sandbox).execute_scan(_scan_req(repo, SPECS_ORDER))
    )
    assert err["error_type"] == INLINE_LIMIT_EXCEEDED
    assert "per-file limit of 10 bytes" in err["error_message"]
    assert sandbox.calls == []


def test_a_file_alone_over_the_total_limit_is_refused_too(repo: Path) -> None:
    # Limits whose per-file cap exceeds the total: a file between the two
    # could never fit any batch, so it is refused rather than sent oversized.
    sandbox = _Sandbox(
        InlineLimits(max_files=10, max_file_bytes=100, max_total_bytes=20)
    )
    err = _whole_batch_error(
        PodLocalInlineXrayBackend(sandbox).execute_scan(_scan_req(repo, ["e.py"]))
    )
    assert err["error_type"] == INLINE_LIMIT_EXCEEDED
    assert "per-file limit of 20 bytes" in err["error_message"]
    assert sandbox.calls == []


def test_a_file_exactly_at_the_per_file_limit_is_sent(repo: Path) -> None:
    size = len((repo / "e.py").read_bytes())
    sandbox = _Sandbox(
        InlineLimits(max_files=10, max_file_bytes=size, max_total_bytes=1 << 20)
    )
    outcome = PodLocalInlineXrayBackend(sandbox).execute_scan(_scan_req(repo, ["e.py"]))
    assert not inline.is_whole_batch_error(outcome.results)
    assert sandbox.calls[0][1] == {"e.py": (repo / "e.py").read_bytes()}


def test_oversized_file_refused_before_read_bytes(tmp_path: Path) -> None:
    """read_candidates checks st_size BEFORE read_bytes — no OOM on huge files."""
    root = tmp_path / "repo"
    root.mkdir()
    big = root / "big.bin"
    big.write_bytes(b"\x00" * 200)
    small = root / "ok.py"
    small.write_bytes(b"x = 1\n")
    limits = InlineLimits(max_files=10, max_file_bytes=100, max_total_bytes=1000)
    with pytest.raises(InlineExecutionError) as exc_info:
        read_candidates(str(root), ["ok.py", "big.bin"], limits=limits)
    assert exc_info.value.error_type == INLINE_LIMIT_EXCEEDED
    assert "200 bytes" in exc_info.value.message


@pytest.mark.parametrize("failing_call", [1, 2, 3])
def test_a_sandbox_error_in_any_batch_is_the_whole_answer(
    repo: Path, failing_call: int
) -> None:
    sandbox = _Sandbox(
        InlineLimits(max_files=2, max_file_bytes=64, max_total_bytes=1 << 20)
    )
    sandbox.fail_on_call = failing_call
    outcome = PodLocalInlineXrayBackend(sandbox).execute_scan(
        _scan_req(repo, SPECS_ORDER)
    )
    assert _whole_batch_error(outcome)["error_type"] == "SandboxError"
    assert outcome.debug_messages is None
    assert len(sandbox.calls) == failing_call  # nothing after the failure


def test_a_batch_answering_the_wrong_number_of_results_is_a_contract_error(
    repo: Path,
) -> None:
    sandbox = _Sandbox(
        InlineLimits(max_files=2, max_file_bytes=64, max_total_bytes=1 << 20)
    )
    sandbox.answer_short_on_call = 2
    err = _whole_batch_error(
        PodLocalInlineXrayBackend(sandbox).execute_scan(_scan_req(repo, SPECS_ORDER))
    )
    assert err["error_type"] == "SandboxContractError"


def test_a_symlink_out_of_the_repository_is_refused_and_nothing_is_sent(
    repo: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"token")
    (repo / "leak.py").symlink_to(secret)
    sandbox = _Sandbox()
    err = _whole_batch_error(
        PodLocalInlineXrayBackend(sandbox).execute_scan(
            _scan_req(repo, ["a.py", "leak.py"])
        )
    )
    assert err["error_type"] == INLINE_READ_REFUSED
    assert "outside the repository" in err["error_message"]
    assert "token" not in err["error_message"]
    assert sandbox.calls == []


def test_a_dotdot_candidate_is_refused(repo: Path) -> None:
    sandbox = _Sandbox()
    err = _whole_batch_error(
        PodLocalInlineXrayBackend(sandbox).execute_scan(
            _scan_req(repo, ["../../etc/passwd"])
        )
    )
    assert err["error_type"] == INLINE_READ_REFUSED
    assert sandbox.calls == []


def test_a_symlink_inside_the_repository_sends_its_target_bytes(repo: Path) -> None:
    (repo / "link.py").symlink_to("a.py")
    sandbox = _Sandbox()
    PodLocalInlineXrayBackend(sandbox).execute_scan(_scan_req(repo, ["link.py"]))
    assert sandbox.calls[0][1] == {"link.py": b"x = 1\n"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_candidate_is_refused(repo: Path) -> None:
    (repo / "a.py").chmod(0)
    try:
        sandbox = _Sandbox()
        err = _whole_batch_error(
            PodLocalInlineXrayBackend(sandbox).execute_scan(_scan_req(repo, ["a.py"]))
        )
    finally:
        (repo / "a.py").chmod(0o644)
    assert err["error_type"] == INLINE_READ_REFUSED
    assert sandbox.calls == []


def test_a_directory_or_missing_candidate_is_simply_not_sent(repo: Path) -> None:
    sandbox = _Sandbox()
    PodLocalInlineXrayBackend(sandbox).execute_scan(
        _scan_req(repo, ["traces", "nope.py", "a.py"])
    )
    assert sandbox.calls[0][1] == {"a.py": b"x = 1\n"}


def test_a_plugin_without_inline_support_is_refused_naming_the_requirement(
    repo: Path,
) -> None:
    class _OldPlugin:
        def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome:
            raise AssertionError("must never be asked")

        def execute_graph_analysis(self, request: XrayGraphRequest) -> Dict[str, Any]:
            raise AssertionError("must never be asked")

    backend = PodLocalInlineXrayBackend(_OldPlugin())
    err = _whole_batch_error(backend.execute_scan(_scan_req(repo, ["a.py"])))
    assert err["error_type"] == INLINE_UNSUPPORTED
    assert "cidx-content-backend >= 0.10.0" in err["error_message"]
    graph = backend.execute_graph_analysis(
        XrayGraphRequest(_EVALUATOR, str(repo), ["a.py"], 60)
    )
    assert graph["error"]["error_type"] == INLINE_UNSUPPORTED


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_one_deadline_spans_every_batch(repo: Path) -> None:
    clock = _Clock()
    limits = InlineLimits(max_files=2, max_file_bytes=64, max_total_bytes=1 << 20)

    def slow_scan(request: XrayScanRequest, root: Path) -> XrayScanOutcome:
        clock.now += 25.5
        return _fake_scan(request, root)

    sandbox = _Sandbox(limits, slow_scan)
    request = _scan_req(repo, SPECS_ORDER, timeout=60)
    outcome = PodLocalInlineXrayBackend(sandbox, clock=clock).execute_scan(request)
    # 60 s, then 34.5 s (ceil 35), then 9 s left; the 4th batch finds it spent.
    assert [req.timeout_seconds for req, _ in sandbox.calls] == [60, 35, 9]
    err = _whole_batch_error(outcome)
    # The local backend's own timeout shape, for the CALLER's timeout.
    assert (
        err
        == api.error_tuple(
            "",
            "XRayCliError",
            api.sanitize_error_message(api.xray_cli_timeout_message(60)),
        )[1][0]
    )


# --------------------------------------------------------------------------
# graph: one run, never split
# --------------------------------------------------------------------------


def test_graph_is_one_inline_run_over_every_file(repo: Path) -> None:
    sandbox = _Sandbox()
    request = XrayGraphRequest(
        _EVALUATOR, str(repo), ["a.py", "e.py", "missing.py"], 60, refine=True
    )
    result = PodLocalInlineXrayBackend(sandbox).execute_graph_analysis(request)
    assert result == {"ok": True, "files": ["a.py", "e.py"]}
    [(sent_request, files)] = sandbox.graph_calls
    assert sent_request is request
    assert files == {"a.py": b"x = 1\n", "e.py": (repo / "e.py").read_bytes()}


@pytest.mark.parametrize(
    "limits,fragment",
    [
        (
            InlineLimits(max_files=1, max_file_bytes=64, max_total_bytes=1 << 20),
            "a graph is never split",
        ),
        (
            InlineLimits(max_files=10, max_file_bytes=50, max_total_bytes=50),
            "a graph is never split",
        ),
        (
            InlineLimits(max_files=10, max_file_bytes=5, max_total_bytes=1 << 20),
            "per-file limit of 5 bytes",
        ),
    ],
)
def test_a_graph_over_any_limit_is_refused_not_split(
    repo: Path, limits: InlineLimits, fragment: str
) -> None:
    sandbox = _Sandbox(limits)
    result = PodLocalInlineXrayBackend(sandbox).execute_graph_analysis(
        XrayGraphRequest(_EVALUATOR, str(repo), ["a.py", "e.py"], 60)
    )
    assert result["ok"] is False
    assert result["error"]["error_type"] == INLINE_LIMIT_EXCEEDED
    assert fragment in result["error"]["error_message"]
    assert sandbox.graph_calls == []


def test_a_graph_exactly_at_the_limits_is_sent(repo: Path) -> None:
    size = len(b"x = 1\n") + len((repo / "e.py").read_bytes())
    limits = InlineLimits(max_files=2, max_file_bytes=64, max_total_bytes=size)
    sandbox = _Sandbox(limits)
    PodLocalInlineXrayBackend(sandbox).execute_graph_analysis(
        XrayGraphRequest(_EVALUATOR, str(repo), ["a.py", "e.py"], 60)
    )
    assert len(sandbox.graph_calls) == 1


def test_a_graph_symlink_escape_is_refused(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "outside.py").write_text("x")
    (repo / "out.py").symlink_to(tmp_path / "outside.py")
    sandbox = _Sandbox()
    result = PodLocalInlineXrayBackend(sandbox).execute_graph_analysis(
        XrayGraphRequest(_EVALUATOR, str(repo), ["out.py"], 60)
    )
    assert result["error"]["error_type"] == INLINE_READ_REFUSED
    assert sandbox.graph_calls == []


# --------------------------------------------------------------------------
# wiring + the local default
# --------------------------------------------------------------------------


@pytest.fixture
def pod_local_registry(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from code_indexer.server.repositories import golden_repo_manager as grm

    def _record(name: str) -> Any:
        if name != repo.name:
            return None
        return SimpleNamespace(repo_url=f"local://{repo.name}", clone_path=str(repo))

    monkeypatch.setattr(
        grm, "get_golden_repo_manager", lambda: SimpleNamespace(get_golden_repo=_record)
    )
    return repo


def test_delegated_pod_local_requests_get_the_inline_backend(
    pod_local_registry: Path,
) -> None:
    sandbox = _Sandbox()
    chosen = xray_wiring.xray_execution_backend_for(
        sandbox, str(pod_local_registry), ["a.py"]
    )
    assert isinstance(chosen, PodLocalInlineXrayBackend)
    chosen.execute_scan(_scan_req(pod_local_registry, ["a.py"]))
    assert sandbox.calls[0][1] == {"a.py": b"x = 1\n"}


def test_the_local_default_is_untouched_for_pod_local_repos(
    pod_local_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """xray_backend=local: the in-pod backend gets the very request it always
    got, and the inline module is never involved."""
    monkeypatch.setattr(content_wiring, "server_content_backend", lambda: None)
    monkeypatch.setattr(xray_wiring, "server_xray_execution_backend", lambda: None)

    def _never(*_a: Any, **_k: Any) -> None:
        raise AssertionError(
            "the inline backend must not be built for xray_backend=local"
        )

    monkeypatch.setattr(inline.PodLocalInlineXrayBackend, "__init__", _never)
    seen: List[XrayScanRequest] = []
    graphs: List[XrayGraphRequest] = []

    def _scan(self: Any, request: XrayScanRequest) -> XrayScanOutcome:
        seen.append(request)
        return XrayScanOutcome([([], [], None)], debug_messages=["local"])

    def _graph(self: Any, request: XrayGraphRequest) -> Dict[str, Any]:
        graphs.append(request)
        return {"where": "local"}

    monkeypatch.setattr(LocalXrayExecutionBackend, "execute_scan", _scan)
    monkeypatch.setattr(LocalXrayExecutionBackend, "execute_graph_analysis", _graph)
    backend = RustNativeBackend()
    specs = _specs(["a.py"])

    def marker(*_a: Any) -> None:
        return None

    result = backend.run_batch(
        evaluator_code=_EVALUATOR,
        file_specs=specs,
        timeout_seconds=17,
        on_process_spawned=marker,
        repo_path=str(pod_local_registry),
    )
    assert result == [([], [], None)]
    assert seen == [
        XrayScanRequest(_EVALUATOR, specs, str(pod_local_registry), 17, marker)
    ]
    assert backend.run_graph_analysis(
        evaluator_code=_EVALUATOR,
        repo_root=str(pod_local_registry),
        file_paths=["a.py"],
        timeout_seconds=9,
        refine=True,
    ) == {"where": "local"}
    assert graphs == [
        XrayGraphRequest(_EVALUATOR, str(pod_local_registry), ["a.py"], 9, True)
    ]


# --------------------------------------------------------------------------
# real xray-cli: inline (batched) == the local backend over the repository
# --------------------------------------------------------------------------

_DEEP_NESTING = """
const DEPTH_THRESHOLD: usize = 2;
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    walk(node, 0, &mut findings);
    findings
}
fn is_cf(kind: &str) -> bool {
    matches!(kind, "if_statement" | "for_statement" | "while_statement" | "with_statement")
}
fn walk(node: &OwnedNode, depth: usize, f: &mut Vec<EvalFinding>) {
    let nd = if is_cf(&node.kind) { depth + 1 } else { depth };
    if nd >= DEPTH_THRESHOLD && is_cf(&node.kind) {
        f.push(EvalFinding { pattern: "deep-nesting".to_string(), line: node.start_line, snippet: String::new() });
    }
    for c in &node.children { walk(c, nd, f); }
}
"""


def _real_local_scan(request: XrayScanRequest, root: Path) -> XrayScanOutcome:
    return LocalXrayExecutionBackend().execute_scan(request)


def test_real_xray_cli_inline_batches_equal_the_local_backend(tmp_path: Path) -> None:
    from tests.unit.xray.conftest import require_xray_cli_binary

    require_xray_cli_binary()
    root = tmp_path / "golden-repos" / "langfuse_proj_bob"
    (root / "pkg").mkdir(parents=True)
    nested = "def f(xs):\n    for x in xs:\n        if x:\n            while x:\n                x -= 1\n"
    (root / "one.py").write_text(nested)
    (root / "pkg" / "two.py").write_text("# pad\n" + nested)
    (root / "three.py").write_text("def g():\n    return 1\n")
    (root / "latin.py").write_bytes(b"# \xe9\n" + nested.encode())
    rels = ["one.py", "pkg/two.py", "missing.py", "three.py", "latin.py", "one.py"]
    request = XrayScanRequest(_DEEP_NESTING, _specs(rels), str(root), 120)

    local = LocalXrayExecutionBackend().execute_scan(request)
    assert any(matches for matches, _e, _m in local.results), (
        "the fixture must produce findings"
    )
    for limits in (
        WIDE,
        InlineLimits(max_files=2, max_file_bytes=1 << 20, max_total_bytes=1 << 20),
    ):
        sandbox = _Sandbox(limits, _real_local_scan)
        inline_outcome = PodLocalInlineXrayBackend(sandbox).execute_scan(request)
        assert inline_outcome.results == local.results
        assert sorted(inline_outcome.debug_messages or []) == sorted(
            local.debug_messages or []
        )
