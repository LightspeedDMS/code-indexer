"""Where one X-Ray request executes: ``xray_execution_backend_for``.

The ONE per-repository decision (server/startup/xray_backend_wiring.py), taken
by ``RustNativeBackend`` before every scan and graph analysis:

* ``xray_backend=delegated`` + a pod-local repository (``local://`` other than
  cidx-meta: Langfuse trace folders) -> the candidate files are read HERE and
  sent inline to the plugin's sandbox (``PodLocalInlineXrayBackend``); the
  evaluator never runs in this pod. cidx-meta and git golden repositories stay
  delegated as before.
* ``xray_backend=local`` + ``content_backend=delegated`` -> xray-cli reads the
  candidates from this pod's disk. When none of them is here (no repository
  mount) the request is refused, loudly, before xray-cli runs; with the files
  present (a pod that still has the mount) it runs as before.
* ``xray_backend=local`` with local content (and the CLI) -> unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

from code_indexer.server.services.delegated_refusal import (
    DelegatedRepositoryUnsupportedError,
)
from code_indexer.server.startup import content_backend_wiring as content_wiring
from code_indexer.server.startup import ripgrep_backend_wiring as rg_wiring
from code_indexer.server.startup import xray_backend_wiring as xray_wiring
from code_indexer.server.startup.xray_backend_wiring import (
    build_configured_xray_execution_backend,
    xray_execution_backend_for,
)
from code_indexer.xray.execution_backend import (
    XrayGraphRequest,
    XrayScanOutcome,
    XrayScanRequest,
)
from code_indexer.xray.rust_backend import LocalXrayExecutionBackend, RustNativeBackend

_EVALUATOR = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }"

POD_LOCAL = "langfuse_proj_alice"
GIT_GOLDEN = "backend-api"
CIDX_META = "cidx-meta"

_URLS = {
    POD_LOCAL: f"local://{POD_LOCAL}",
    GIT_GOLDEN: "https://git.example.com/acme/backend-api.git",
    CIDX_META: "local://cidx-meta",
}


class _DelegatedPlugin:
    """The parked delegated X-Ray backend: records every request."""

    inline_limits = SimpleNamespace(
        max_files=10000,
        max_file_bytes=4 * 1024 * 1024,
        max_total_bytes=12 * 1024 * 1024,
    )

    def __init__(self) -> None:
        self.scans: List[XrayScanRequest] = []
        self.graphs: List[XrayGraphRequest] = []
        self.inline_scans: List[Tuple[XrayScanRequest, Dict[str, bytes]]] = []
        self.inline_graphs: List[Tuple[XrayGraphRequest, Dict[str, bytes]]] = []

    def execute_scan_inline(
        self, request: XrayScanRequest, files: Dict[str, bytes]
    ) -> XrayScanOutcome:
        self.inline_scans.append((request, dict(files)))
        return XrayScanOutcome([([], [], None) for _ in request.file_specs])

    def execute_graph_analysis_inline(
        self, request: XrayGraphRequest, files: Dict[str, bytes]
    ) -> Dict[str, Any]:
        self.inline_graphs.append((request, dict(files)))
        return {"error": None, "where": "inline"}

    def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome:
        self.scans.append(request)
        return XrayScanOutcome([([], [], None) for _ in request.file_specs])

    def execute_graph_analysis(self, request: XrayGraphRequest) -> Dict[str, Any]:
        self.graphs.append(request)
        return {"error": None, "where": "delegated"}


class _DelegatedContent:
    """A NON-local content backend that must never be asked."""

    is_local = False

    def manifest(self, *_a: Any, **_k: Any) -> Any:
        raise AssertionError("pod-local repository sent to the content backend")

    def fetch(self, *_a: Any, **_k: Any) -> Any:
        raise AssertionError("pod-local repository sent to the content backend")


class _DelegatedRipgrep:
    is_local = False

    async def run(self, **_k: Any) -> Any:
        raise AssertionError("pod-local repository sent to delegated ripgrep")

    async def supports_pcre2(self) -> bool:
        return True


class _LocalRecorder:
    """Stands in for xray-cli in this pod (LocalXrayExecutionBackend)."""

    def __init__(self) -> None:
        self.scans: List[XrayScanRequest] = []
        self.graphs: List[XrayGraphRequest] = []


@pytest.fixture
def local_exec(monkeypatch: pytest.MonkeyPatch) -> _LocalRecorder:
    recorder = _LocalRecorder()

    def _scan(self: Any, request: XrayScanRequest) -> XrayScanOutcome:
        recorder.scans.append(request)
        return XrayScanOutcome([([], [], None) for _ in request.file_specs])

    def _graph(self: Any, request: XrayGraphRequest) -> Dict[str, Any]:
        recorder.graphs.append(request)
        return {"error": None, "where": "local"}

    monkeypatch.setattr(LocalXrayExecutionBackend, "execute_scan", _scan)
    monkeypatch.setattr(LocalXrayExecutionBackend, "execute_graph_analysis", _graph)
    return recorder


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """golden-repos/ with a pod-local, a git and the cidx-meta repo registered."""
    from code_indexer.server.repositories import golden_repo_manager as grm

    base = tmp_path / "golden-repos"
    for alias in _URLS:
        (base / alias).mkdir(parents=True)
    (base / POD_LOCAL / "trace.py").write_text("def handler():\n    return 1\n")

    def _record(name: str) -> Any:
        if name not in _URLS:
            return None
        return SimpleNamespace(repo_url=_URLS[name], clone_path=str(base / name))

    manager = SimpleNamespace(get_golden_repo=_record)
    monkeypatch.setattr(grm, "get_golden_repo_manager", lambda: manager)
    return base


def _park(
    monkeypatch: pytest.MonkeyPatch, *, content: Any = None, xray: Any = None
) -> None:
    monkeypatch.setattr(content_wiring, "server_content_backend", lambda: content)
    monkeypatch.setattr(xray_wiring, "server_xray_execution_backend", lambda: xray)


def _scan(backend: RustNativeBackend, repo: Path, rels: List[str]) -> Any:
    return backend.run_batch(
        evaluator_code=_EVALUATOR,
        file_specs=[{"file_path": r, "lang": "python"} for r in rels],
        repo_path=str(repo),
    )


def _graph(backend: RustNativeBackend, repo: Path, rels: List[str]) -> Any:
    return backend.run_graph_analysis(
        evaluator_code=_EVALUATOR, repo_root=str(repo), file_paths=rels
    )


def _forbid_disk_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("presence probe outside delegated content")

    monkeypatch.setattr(Path, "is_file", _no)


# --------------------------------------------------------------------------
# delegated execution: pod-local repositories are read here, run in the sandbox
# --------------------------------------------------------------------------

TRACE = b"def handler():\n    return 1\n"


@pytest.mark.parametrize("run", [_scan, _graph])
def test_pod_local_repo_runs_in_the_sandbox_with_its_files_inline(
    golden: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_exec: _LocalRecorder,
    run: Any,
) -> None:
    plugin = _DelegatedPlugin()
    _park(monkeypatch, content=_DelegatedContent(), xray=plugin)
    run(RustNativeBackend(), golden / POD_LOCAL, ["trace.py"])
    # The caller's evaluator never runs in this pod ...
    assert local_exec.scans == [] and local_exec.graphs == []
    # ... nor goes the mounted way (the sandbox cannot mount this repo) ...
    assert plugin.scans == [] and plugin.graphs == []
    # ... it goes inline, with the bytes this pod read.
    sent = [files for _, files in plugin.inline_scans + plugin.inline_graphs]
    assert sent == [{"trace.py": TRACE}]


def test_pod_local_snapshot_path_is_read_from_the_snapshot(
    golden: Path, monkeypatch: pytest.MonkeyPatch, local_exec: _LocalRecorder
) -> None:
    snapshot = golden / ".versioned" / POD_LOCAL / "v_1700000000"
    snapshot.mkdir(parents=True)
    (snapshot / "trace.py").write_text("x = 1\n")
    plugin = _DelegatedPlugin()
    _park(monkeypatch, content=_DelegatedContent(), xray=plugin)
    _scan(RustNativeBackend(), snapshot, ["trace.py"])
    assert plugin.scans == [] and local_exec.scans == []
    [(request, files)] = plugin.inline_scans
    assert request.repo_path == str(snapshot)
    assert files == {"trace.py": b"x = 1\n"}


@pytest.mark.parametrize("alias", [GIT_GOLDEN, CIDX_META])
@pytest.mark.parametrize("run", [_scan, _graph])
def test_golden_repos_stay_delegated(
    golden: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_exec: _LocalRecorder,
    alias: str,
    run: Any,
) -> None:
    plugin = _DelegatedPlugin()
    _park(monkeypatch, content=_DelegatedContent(), xray=plugin)
    run(RustNativeBackend(), golden / alias, ["a.py"])
    assert len(plugin.scans) + len(plugin.graphs) == 1
    assert local_exec.scans == [] and local_exec.graphs == []


def test_unregistered_repo_stays_delegated(
    golden: Path, monkeypatch: pytest.MonkeyPatch, local_exec: _LocalRecorder
) -> None:
    """Activated repos and anything the registry cannot describe: as today."""
    plugin = _DelegatedPlugin()
    _park(monkeypatch, content=_DelegatedContent(), xray=plugin)
    activated = golden.parent / "activated-repos" / "alice" / "my-repo"
    _scan(RustNativeBackend(), activated, ["a.py"])
    assert len(plugin.scans) == 1 and local_exec.scans == []


def test_pod_local_routing_is_logged(
    golden: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_exec: _LocalRecorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _park(monkeypatch, content=_DelegatedContent(), xray=_DelegatedPlugin())
    with caplog.at_level(logging.INFO, logger=xray_wiring.__name__):
        _scan(RustNativeBackend(), golden / POD_LOCAL, ["trace.py"])
    assert "pod-local repository" in caplog.text


# --------------------------------------------------------------------------
# in-pod execution under delegated content: refused without the repository
# --------------------------------------------------------------------------


@pytest.mark.parametrize("run", [_scan, _graph])
def test_mount_absent_is_refused_before_xray_cli_runs(
    golden: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_exec: _LocalRecorder,
    run: Any,
) -> None:
    _park(monkeypatch, content=_DelegatedContent(), xray=None)
    with pytest.raises(DelegatedRepositoryUnsupportedError) as raised:
        run(RustNativeBackend(), golden / GIT_GOLDEN, ["src/a.py", "src/b.py"])
    message = str(raised.value)
    assert "xray_backend='local'" in message
    assert "content_backend='delegated'" in message
    assert "none of the 2 candidate file(s)" in message
    assert local_exec.scans == [] and local_exec.graphs == []


@pytest.mark.parametrize("run", [_scan, _graph])
def test_mount_present_runs_in_this_pod(
    golden: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_exec: _LocalRecorder,
    run: Any,
) -> None:
    """Rollout: delegated content on a pod that still has the mount. One
    candidate on disk is enough; a missing one is xray-cli's per-file error."""
    repo = golden / GIT_GOLDEN
    (repo / "src").mkdir()
    (repo / "src" / "b.py").write_text("x = 1\n")
    _park(monkeypatch, content=_DelegatedContent(), xray=None)
    run(RustNativeBackend(), repo, ["src/a.py", "src/b.py"])
    assert len(local_exec.scans) + len(local_exec.graphs) == 1


def test_pod_local_repo_is_never_refused(
    golden: Path, monkeypatch: pytest.MonkeyPatch, local_exec: _LocalRecorder
) -> None:
    _park(monkeypatch, content=_DelegatedContent(), xray=None)
    _scan(RustNativeBackend(), golden / POD_LOCAL, ["trace.py", "gone.py"])
    # Even with none of its candidates present it is this pod's own repo.
    _scan(RustNativeBackend(), golden / POD_LOCAL, ["gone.py"])
    assert len(local_exec.scans) == 2


def test_no_candidates_is_not_refused(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _park(monkeypatch, content=_DelegatedContent(), xray=None)
    assert xray_execution_backend_for(None, str(golden / GIT_GOLDEN), []) is None


# --------------------------------------------------------------------------
# local configuration: unchanged
# --------------------------------------------------------------------------


@pytest.mark.parametrize("alias", [POD_LOCAL, GIT_GOLDEN, "unregistered"])
def test_local_config_executes_here_without_probing(
    golden: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_exec: _LocalRecorder,
    alias: str,
) -> None:
    _park(monkeypatch, content=None, xray=None)
    _forbid_disk_probe(monkeypatch)
    _scan(RustNativeBackend(), golden / alias, ["missing.py"])
    _graph(RustNativeBackend(), golden / alias, ["missing.py"])
    assert len(local_exec.scans) == 1 and len(local_exec.graphs) == 1


def test_local_content_with_delegated_execution_stays_delegated_for_git(
    golden: Path, monkeypatch: pytest.MonkeyPatch, local_exec: _LocalRecorder
) -> None:
    plugin = _DelegatedPlugin()
    _park(monkeypatch, content=None, xray=plugin)
    _forbid_disk_probe(monkeypatch)
    _scan(RustNativeBackend(), golden / GIT_GOLDEN, ["a.py"])
    assert len(plugin.scans) == 1 and local_exec.scans == []


def test_no_repo_path_keeps_the_configured_backend() -> None:
    plugin = _DelegatedPlugin()
    assert xray_execution_backend_for(plugin, None, ["a.py"]) is plugin
    assert xray_execution_backend_for(None, None, ["a.py"]) is None


# --------------------------------------------------------------------------
# startup: warn, never refuse
# --------------------------------------------------------------------------


def test_startup_warns_for_local_xray_under_delegated_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = SimpleNamespace(xray_backend="local", content_backend="delegated")
    with caplog.at_level(logging.WARNING, logger=xray_wiring.__name__):
        assert build_configured_xray_execution_backend(config) is None
    assert "xray-cli runs in THIS pod" in caplog.text
    assert "refused" in caplog.text


@pytest.mark.parametrize(
    "config",
    [
        SimpleNamespace(xray_backend="local", content_backend="local"),
        SimpleNamespace(xray_backend="local"),
        SimpleNamespace(),
    ],
)
def test_startup_is_quiet_under_local_content(
    caplog: pytest.LogCaptureFixture, config: Any
) -> None:
    with caplog.at_level(logging.WARNING, logger=xray_wiring.__name__):
        assert build_configured_xray_execution_backend(config) is None
    assert caplog.text == ""


# --------------------------------------------------------------------------
# end to end: a pod-local repo is read here, never through the content or ripgrep seams,
# and its evaluator runs only in the sandbox
# --------------------------------------------------------------------------


def test_pod_local_search_reads_here_and_runs_in_the_sandbox(
    golden: Path, monkeypatch: pytest.MonkeyPatch, local_exec: _LocalRecorder
) -> None:
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine, _phase1_regex_service

    plugin = _DelegatedPlugin()
    _park(monkeypatch, content=_DelegatedContent(), xray=plugin)
    ripgrep = _DelegatedRipgrep()
    monkeypatch.setattr(rg_wiring, "server_ripgrep_backend", lambda: ripgrep)

    result = XRaySearchEngine().run(
        repo_path=golden / POD_LOCAL,
        driver_regex=r"\.py$",
        evaluator_code=_EVALUATOR,
        search_target="filename",
    )

    assert result["files_total"] == 1
    assert plugin.scans == [] and local_exec.scans == []
    assert [
        ([s["file_path"] for s in scan.file_specs], files)
        for scan, files in plugin.inline_scans
    ] == [(["trace.py"], {"trace.py": TRACE})]
    # The content driver targets this pod's repository too, not ripgrep there.
    service = _phase1_regex_service(golden / POD_LOCAL)
    assert service._ripgrep_backend is not ripgrep
    assert service._ripgrep_backend.is_local


@pytest.mark.parametrize("run", [_scan, _graph])
def test_pod_local_repo_executes_in_this_pod_under_delegation(
    golden: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_exec: _LocalRecorder,
    run: Any,
) -> None:
    plugin = _DelegatedPlugin()
    _park(monkeypatch, content=_DelegatedContent(), xray=plugin)
    run(RustNativeBackend(), golden / POD_LOCAL, ["trace.py"])
    assert plugin.scans == [] and plugin.graphs == []
    assert len(local_exec.scans) + len(local_exec.graphs) == 1


def test_pod_local_search_never_touches_a_delegated_seam(
    golden: Path, monkeypatch: pytest.MonkeyPatch, local_exec: _LocalRecorder
) -> None:
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine, _phase1_regex_service

    plugin = _DelegatedPlugin()
    _park(monkeypatch, content=_DelegatedContent(), xray=plugin)
    ripgrep = _DelegatedRipgrep()
    monkeypatch.setattr(rg_wiring, "server_ripgrep_backend", lambda: ripgrep)

    result = XRaySearchEngine().run(
        repo_path=golden / POD_LOCAL,
        driver_regex=r"\.py$",
        evaluator_code=_EVALUATOR,
        search_target="filename",
    )

    assert result["files_total"] == 1
    assert plugin.scans == []
    assert [[s["file_path"] for s in scan.file_specs] for scan in local_exec.scans] == [
        ["trace.py"]
    ]
    # The content driver targets this pod's repository too, not ripgrep there.
    service = _phase1_regex_service(golden / POD_LOCAL)
    assert service._ripgrep_backend is not ripgrep
    assert service._ripgrep_backend.is_local


def test_pod_local_snapshot_path_executes_in_this_pod(
    golden: Path, monkeypatch: pytest.MonkeyPatch, local_exec: _LocalRecorder
) -> None:
    snapshot = golden / ".versioned" / POD_LOCAL / "v_1700000000"
    snapshot.mkdir(parents=True)
    (snapshot / "trace.py").write_text("x = 1\n")
    plugin = _DelegatedPlugin()
    _park(monkeypatch, content=_DelegatedContent(), xray=plugin)
    _scan(RustNativeBackend(), snapshot, ["trace.py"])
    assert plugin.scans == []
    assert [s.repo_path for s in local_exec.scans] == [str(snapshot)]
