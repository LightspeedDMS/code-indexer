"""The X-Ray seam as the X-Ray feature resolves it: the delegate through entry points.

``xray_backend='delegated'`` reaches its ``XrayBackend`` only through the seam
resolver. Here the delegate is the testkit's fake sandbox-mcp -- two replicas
sharing one result store -- found exactly as an installed delegate is; it
passes the X-Ray conformance suite (with the inline extension) as the X-Ray
feature's server wiring (``cidx_xray.server``) builds it, and the feature's pod-local wrapper
(``PodLocalInlineXrayBackend``: files read here, sent inline in batches)
passes the same suite over it.

``local`` is the X-Ray feature's own engine (``cidx_xray``, xray-cli), which
the feature builds per request; it is not resolved from the group. Running
the suite against it needs xray-cli, rustc and a Rust evaluator, and belongs
with the feature's own tests.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Tuple

import pytest

from cidx_sdk.seams.xray import XrayGraphRequest
from cidx_testkit.conformance.scip import directory_writer
from cidx_testkit.conformance.xray import (
    XrayBackendContract,
    XrayInlineContract,
    XraySubject,
)
from cidx_testkit.fakes.sandbox_mcp import (
    FakeSandboxMcp,
    FakeSandboxXrayBackend,
    SandboxResultStore,
    SandboxState,
    fake_evaluator,
    register_as_delegate,
    unregister_delegate,
)
from cidx_xray.inline_execution import PodLocalInlineXrayBackend
from cidx_xray.server import backend_plugin as xray_backend_plugin
from cidx_xray.server.backend_wiring import build_configured_xray_execution_backend

DELEGATED = SimpleNamespace(xray_backend="delegated")


@pytest.fixture(autouse=True)
def _fresh_resolver(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient install: only what a test registers."""
    monkeypatch.setattr(xray_backend_plugin, "entry_points", lambda **_: [])
    monkeypatch.delenv(xray_backend_plugin.ENV_VAR, raising=False)
    xray_backend_plugin.reset_cache()
    yield
    xray_backend_plugin.reset_cache()


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[Tuple[SandboxState, Path]]:
    state = SandboxState(
        results=SandboxResultStore(chunk_bytes=128), inline_output_bytes=256
    )
    repo = tmp_path / "golden-repos" / "example-repo"
    state.serve(str(repo), repo)
    registered = register_as_delegate(
        FakeSandboxMcp(state, name="a"), FakeSandboxMcp(state, name="b")
    )
    try:
        yield state, repo
    finally:
        unregister_delegate(registered)


class TestResolvedXrayBackend(XrayInlineContract):
    @pytest.fixture
    def xray_subject(self, sandbox: Tuple[SandboxState, Path]) -> XraySubject:
        _state, repo = sandbox
        backend = build_configured_xray_execution_backend(DELEGATED)
        assert isinstance(backend, FakeSandboxXrayBackend)
        return XraySubject(
            backend=backend,
            repo_root=str(repo),
            write=directory_writer(repo),
            evaluator_for=fake_evaluator,
            local_root=repo,
        )


class TestPodLocalInlineOverTheDelegate(XrayBackendContract):
    """The pod-local path: candidates read here, sent inline, batched under
    the delegate's limits (two files per run) and merged back in spec order."""

    @pytest.fixture
    def xray_subject(
        self, sandbox: Tuple[SandboxState, Path], tmp_path: Path
    ) -> XraySubject:
        state, _repo = sandbox
        state.inline_max_files = 2
        delegated = build_configured_xray_execution_backend(DELEGATED)
        local = tmp_path / "pod-local-repo"
        return XraySubject(
            backend=PodLocalInlineXrayBackend(delegated),
            repo_root=str(local),
            write=directory_writer(local),
            evaluator_for=fake_evaluator,
            local_root=local,
        )


def test_the_pod_local_scan_was_batched_inline(
    sandbox: Tuple[SandboxState, Path], tmp_path: Path
) -> None:
    state, _repo = sandbox
    state.inline_max_files = 1
    replicas = [FakeSandboxMcp(state, name="a"), FakeSandboxMcp(state, name="b")]
    local = tmp_path / "pod-local-repo"
    write = directory_writer(local)
    write("a.py", b"needle\n")
    write("b.py", b"none\n")
    backend = PodLocalInlineXrayBackend(FakeSandboxXrayBackend(replicas))
    subject = XraySubject(backend, str(local), write, fake_evaluator, local)
    outcome = XrayBackendContract.scan(
        subject, ["a.py", "b.py"], fake_evaluator("needle")
    )
    assert [len(m) for m, _, _ in outcome.results] == [1, 0]
    runs = [
        r
        for replica in replicas
        for r in replica.requests
        if r[0] != "sandbox_result_fetch"
    ]
    assert [r[1]["profile"] for r in runs] == ["xray-inline", "xray-inline"]


def test_a_pod_local_graph_over_the_inline_limit_is_refused_not_split(
    sandbox: Tuple[SandboxState, Path], tmp_path: Path
) -> None:
    state, _repo = sandbox
    state.inline_max_files = 1
    local = tmp_path / "pod-local-repo"
    write = directory_writer(local)
    write("a.py", b"needle\n")
    write("b.py", b"needle\n")
    backend = PodLocalInlineXrayBackend(
        build_configured_xray_execution_backend(DELEGATED)
    )
    result = backend.execute_graph_analysis(
        XrayGraphRequest(fake_evaluator("needle"), str(local), ["a.py", "b.py"], 60)
    )
    assert result["ok"] is False
    assert result["error"]["error_type"] == "SandboxInlineLimitExceeded"


def test_local_selects_no_resolved_backend() -> None:
    """Under ``local`` the wiring builds nothing: the feature runs its own
    engine, so nothing is resolved from the group."""
    assert build_configured_xray_execution_backend(SimpleNamespace()) is None
