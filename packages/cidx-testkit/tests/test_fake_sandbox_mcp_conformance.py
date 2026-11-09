"""The fake sandbox-mcp's SCIP and X-Ray clients pass their seams' suites,
over two replicas sharing one result store, and page parked results."""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import pytest

from cidx_sdk.seams.scip import STATUS_FAILED, STATUS_OK, ScipBuildRequest
from cidx_sdk.seams.xray import XrayScanRequest
from cidx_testkit.conformance.scip import (
    ScipBackendContract,
    ScipSubject,
    directory_writer,
)
from cidx_testkit.conformance.xray import XrayInlineContract, XraySubject
from cidx_testkit.fakes.sandbox_fake import (
    RESULT_FETCH_TOOL,
    RUN_PROFILE_TOOL,
    FakeSandboxError,
    FakeSandboxMcp,
    FakeSandboxScipBackend,
    FakeSandboxXrayBackend,
    SandboxResultStore,
    SandboxState,
    evaluator_needle,
    fake_evaluator,
)

_REPO = "/srv/example/golden-repos/example-repo"
_ALIAS = "example-repo"


def _replicas(state: SandboxState) -> Tuple[FakeSandboxMcp, FakeSandboxMcp]:
    return FakeSandboxMcp(state, name="a"), FakeSandboxMcp(state, name="b")


def _state(tmp_path: Path) -> Tuple[SandboxState, Path]:
    state = SandboxState(
        results=SandboxResultStore(chunk_bytes=256), inline_output_bytes=512
    )
    state.unsupported_languages.add("example-unsupported")
    directory = state.serve(_REPO, tmp_path / "repo")
    state.serve(_ALIAS, directory)
    return state, directory


class TestFakeSandboxScipBackend(ScipBackendContract):
    @pytest.fixture
    def scip_subject(self, tmp_path: Path) -> ScipSubject:
        state, directory = _state(tmp_path)
        return ScipSubject(
            backend=FakeSandboxScipBackend(_replicas(state)),
            repo_path=_REPO,
            golden_alias=_ALIAS,
            write=directory_writer(directory),
        )


class TestFakeSandboxXrayBackend(XrayInlineContract):
    @pytest.fixture
    def xray_subject(self, tmp_path: Path) -> XraySubject:
        state, directory = _state(tmp_path)
        return XraySubject(
            backend=FakeSandboxXrayBackend(_replicas(state)),
            repo_root=_REPO,
            write=directory_writer(directory),
            evaluator_for=fake_evaluator,
            local_root=directory,
        )


def _big_project(directory: Path) -> None:
    for number in range(100):
        (directory / f"m{number:03d}.py").write_text("x = 1\n")


def _tools(replica: FakeSandboxMcp) -> List[str]:
    return [tool for tool, _ in replica.requests]


def test_a_parked_result_is_paged_across_replicas(tmp_path: Path) -> None:
    state, directory = _state(tmp_path)
    _big_project(directory)
    first, second = _replicas(state)
    outcome = FakeSandboxScipBackend([first, second]).build_index(
        ScipBuildRequest(_REPO, _ALIAS, ".", "pip", "python")
    )
    assert outcome.status == STATUS_OK, outcome.error
    assert _tools(first)[0] == RUN_PROFILE_TOOL
    pages = [
        tool for tool in _tools(first) + _tools(second) if tool == RESULT_FETCH_TOOL
    ]
    assert len(pages) > 1
    assert RESULT_FETCH_TOOL in _tools(second), "no page was read on the other replica"


def test_a_replica_keeping_results_to_itself_cannot_serve_the_page(
    tmp_path: Path,
) -> None:
    """The per-node-state defect the shared store exists to prevent."""
    state, directory = _state(tmp_path)
    _big_project(directory)
    first = FakeSandboxMcp(state, results=SandboxResultStore(chunk_bytes=256))
    second = FakeSandboxMcp(state, results=SandboxResultStore(chunk_bytes=256))
    with pytest.raises(FakeSandboxError) as caught:
        FakeSandboxScipBackend([first, second]).run(
            "scip", {"repo_path": _REPO, "project_path": ".", "language": "python"}
        )
    assert caught.value.status_code == 404


def test_result_fetch_pages_are_bounded_and_ordered(tmp_path: Path) -> None:
    store = SandboxResultStore(chunk_bytes=4)
    handle, chunks = store.park(b"0123456789")
    assert chunks == 3
    assert [store.page(handle, n)["next_chunk"] for n in range(3)] == [1, 2, None]
    with pytest.raises(FakeSandboxError) as caught:
        store.page(handle, 3)
    assert caught.value.status_code == 400


def test_no_capacity_is_a_backpressure_outcome(tmp_path: Path) -> None:
    state, _ = _state(tmp_path)
    state.busy = True
    outcome = FakeSandboxScipBackend(_replicas(state)).build_index(
        ScipBuildRequest(_REPO, _ALIAS, ".", "pip", "python")
    )
    assert outcome.status == STATUS_FAILED
    assert outcome.backpressure is True


def test_an_unhealthy_sandbox_is_unavailable(tmp_path: Path) -> None:
    state, _ = _state(tmp_path)
    state.healthy = False
    assert FakeSandboxScipBackend(_replicas(state)).is_available() is False


def test_an_inline_request_over_its_limits_is_a_whole_batch_error(
    tmp_path: Path,
) -> None:
    state, _ = _state(tmp_path)
    state.inline_max_files = 1
    outcome = FakeSandboxXrayBackend(_replicas(state)).execute_scan_inline(
        XrayScanRequest(
            fake_evaluator("x"),
            [{"file_path": "a.py"}, {"file_path": "b.py"}],
            None,
            60,
        ),
        {"a.py": b"x\n", "b.py": b"x\n"},
    )
    assert len(outcome.results) == 1
    assert outcome.results[0][1][0]["file_path"] == ""


def test_the_fake_evaluator_round_trips_its_needle() -> None:
    assert evaluator_needle(fake_evaluator('say "hi"')) == 'say "hi"'
    assert evaluator_needle("no entry point") is None
