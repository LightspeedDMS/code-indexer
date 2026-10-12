"""The ripgrep execution seam: contract, wiring, and the fail-loud selection.

RegexSearchService keeps every byte budget, deadline and parser; only the
execution of the ripgrep argv moves behind a backend. These tests pin the parts
that would otherwise rot silently -- the no-match/error split, the truncation
signal, and the rule that a delegated backend's ripgrep is never probed on this
container's PATH.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService
from code_indexer.server.storage.shared.ripgrep_backend import (
    LocalRipgrepBackend,
    RipgrepBackend,
    RipgrepBackendFactory,
    RipgrepRun,
)


class _RecordingBackend:
    """A delegated-style backend that records what it was handed."""

    def __init__(self, run: Optional[RipgrepRun] = None) -> None:
        self.calls: list = []
        self._run = run or RipgrepRun(
            exit_code=1, stderr_output="", timed_out=False, timeout_seconds=30.0
        )

    @property
    def is_local(self) -> bool:
        return False

    async def run(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> RipgrepRun:
        self.calls.append(
            {
                "argv": list(argv),
                "working_dir": working_dir,
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes,
            }
        )
        return self._run

    async def supports_pcre2(self) -> bool:
        return True


# --- the result contract -----------------------------------------------------


def test_exit_1_with_no_stderr_is_no_matches_not_an_error() -> None:
    """Bug #173: ripgrep says 'searched fine, matched nothing' with exit 1."""
    run = RipgrepRun(
        exit_code=1, stderr_output="", timed_out=False, timeout_seconds=30.0
    )
    assert run.found_no_matches is True


@pytest.mark.parametrize(
    "exit_code,stderr",
    [(2, "regex parse error"), (1, "some stderr"), (0, ""), (None, "")],
)
def test_everything_else_is_not_no_matches(exit_code, stderr) -> None:
    run = RipgrepRun(
        exit_code=exit_code,
        stderr_output=stderr,
        timed_out=False,
        timeout_seconds=30.0,
    )
    assert run.found_no_matches is False


def test_output_capped_defaults_false_and_is_carried() -> None:
    """The truncation signal must survive the hop, or a partial scan reads as
    complete."""
    assert (
        RipgrepRun(
            exit_code=0, stderr_output="", timed_out=False, timeout_seconds=1.0
        ).output_capped
        is False
    )
    assert (
        RipgrepRun(
            exit_code=0,
            stderr_output="",
            timed_out=False,
            timeout_seconds=1.0,
            output_capped=True,
        ).output_capped
        is True
    )


# --- factory: fail loud, never silently local --------------------------------


def test_local_is_the_default() -> None:
    backend = RipgrepBackendFactory.create()
    assert isinstance(backend, LocalRipgrepBackend)
    assert backend.is_local is True
    assert isinstance(backend, RipgrepBackend)


def test_delegated_without_a_plugin_raises_rather_than_falling_back() -> None:
    with pytest.raises(RuntimeError, match="no delegated backend"):
        RipgrepBackendFactory.create("delegated")


def test_delegated_returns_the_injected_backend() -> None:
    injected = _RecordingBackend()
    assert (
        RipgrepBackendFactory.create("delegated", delegated_backend=injected)
        is injected
    )


def test_unknown_backend_name_raises() -> None:
    with pytest.raises(RuntimeError, match="Unknown ripgrep_backend"):
        RipgrepBackendFactory.create("ripgrep-but-fancy")


# --- wiring into the service -------------------------------------------------


def test_service_defaults_to_the_local_backend(tmp_path: Path) -> None:
    svc = RegexSearchService(tmp_path)
    assert isinstance(svc._ripgrep_backend, LocalRipgrepBackend)


def test_delegated_backend_is_not_probed_on_this_containers_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With rg absent HERE, a delegated backend must still select ripgrep.

    Probing this PATH would 'discover' no rg and silently drop to the linear
    grep scan -- running the wrong engine against a remote ripgrep.
    """
    import code_indexer.global_repos.regex_search as mod

    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    svc = RegexSearchService(tmp_path, ripgrep_backend=_RecordingBackend())
    assert svc._search_engine == "ripgrep"


def test_local_backend_with_no_rg_still_degrades_to_grep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local path keeps its existing behaviour exactly."""
    import code_indexer.global_repos.regex_search as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda name: None if name == "rg" else "/bin/grep"
    )
    svc = RegexSearchService(tmp_path)
    assert svc._search_engine == "grep"


@pytest.mark.asyncio
async def test_search_executes_through_the_backend(tmp_path: Path) -> None:
    """The argv, cwd and byte ceiling all reach the backend."""
    (tmp_path / "f.py").write_text("needle here\n")
    backend = _RecordingBackend()
    svc = RegexSearchService(tmp_path, ripgrep_backend=backend)
    await svc.search(pattern="needle", max_results=5)
    assert backend.calls, "the service did not route through the backend"
    call = backend.calls[0]
    assert call["argv"][0] == "rg" and "--json" in call["argv"]
    assert "needle" in call["argv"]
    assert call["working_dir"] == str(tmp_path.resolve())
    assert call["max_output_bytes"] > 0
    assert call["timeout_seconds"] > 0


@pytest.mark.asyncio
async def test_pcre2_question_goes_to_the_backend_not_a_local_fork(
    tmp_path: Path,
) -> None:
    """A delegated backend answers for ITS ripgrep; we must not fork here."""
    import code_indexer.global_repos.regex_search as mod

    svc = RegexSearchService(tmp_path, ripgrep_backend=_RecordingBackend())

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("forked a local `rg --pcre2-version` for a remote backend")

    object.__setattr__(svc, "_detect_pcre2_support", _boom)
    assert await svc._pcre2_available() is True
    assert mod is not None


# --- the local backend's mapping from the executor result --------------------


class _FakeExecResult:
    def __init__(self, **kw):
        self.exit_code = kw.get("exit_code", 0)
        self.stderr_output = kw.get("stderr_output", "")
        self.timed_out = kw.get("timed_out", False)
        self.timeout_seconds = kw.get("timeout_seconds", 30)
        self.error_message = kw.get("error_message", "")
        self.output_capped = kw.get("output_capped", False)
        self.status = kw.get("status")


async def _run_local_with(monkeypatch: pytest.MonkeyPatch, exec_result):
    """Drive LocalRipgrepBackend.run with a stubbed executor.

    Async, and awaited from `@pytest.mark.asyncio` tests, rather than driven by a
    bare ``asyncio.run``. The backend offloads ``executor.shutdown`` through
    ``anyio.to_thread.run_sync``, which starts an ``asyncio_N`` worker in anyio's
    pool; under a loop this module owns and closes, that non-daemon worker
    outlives the session and trips the Bug #1800 leak guard at teardown. Letting
    the suite's own async runner own the loop keeps the pool within its lifecycle.
    """
    import code_indexer.server.services.subprocess_executor as se

    captured = {}

    class _StubExecutor:
        def __init__(self, max_workers=2):
            pass

        async def execute_with_limits(self, **kw):
            captured.update(kw)
            return exec_result

        def shutdown(self, wait=True, cancel_futures=False):
            captured["shutdown_wait"] = wait

    monkeypatch.setattr(se, "SubprocessExecutor", _StubExecutor)
    out = await LocalRipgrepBackend().run(
        argv=["rg", "--json", "-e", "x"],
        working_dir="/tmp",
        output_file_path="/tmp/out.txt",
        timeout_seconds=12.3,
        max_output_bytes=999,
    )
    out_captured = dict(captured)
    return out, out_captured  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_local_backend_carries_output_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A byte-cap kill must survive the hop; otherwise a truncated scan is
    reported to users as a complete one."""
    import code_indexer.server.services.subprocess_executor as se

    res = _FakeExecResult(output_capped=True, status=se.ExecutionStatus.SUCCESS)
    run, _ = await _run_local_with(monkeypatch, res)
    assert run.output_capped is True


@pytest.mark.asyncio
async def test_local_backend_carries_is_error_from_executor_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_indexer.server.services.subprocess_executor as se

    err, _ = await _run_local_with(
        monkeypatch,
        _FakeExecResult(
            exit_code=2, stderr_output="bad", status=se.ExecutionStatus.ERROR
        ),
    )
    assert err.is_error is True and err.found_no_matches is False

    ok, _ = await _run_local_with(
        monkeypatch, _FakeExecResult(exit_code=0, status=se.ExecutionStatus.SUCCESS)
    )
    assert ok.is_error is False


@pytest.mark.asyncio
async def test_local_backend_rounds_the_deadline_up_and_drains_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rounding a 12.3s remaining deadline down to 12 would shorten it."""
    import code_indexer.server.services.subprocess_executor as se

    _, captured = await _run_local_with(
        monkeypatch, _FakeExecResult(status=se.ExecutionStatus.SUCCESS)
    )
    assert captured["timeout_seconds"] == 13
    assert captured["max_output_bytes"] == 999
    assert captured["command"] == ["rg", "--json", "-e", "x"]
    # wait=True, matching the idiom the call sites used before the seam.
    assert captured["shutdown_wait"] is True


# --- a pod that holds only the index state -----------------------------------
#
# Under a delegated backend the repository is not on this disk, so nothing in
# a search may ask this disk about it: not the search path, not the files.


class _WritingBackend(_RecordingBackend):
    """Answers as workspace-mcp would: ripgrep's --json lines, or its error."""

    def __init__(self, stdout: str = "", run: Optional[RipgrepRun] = None) -> None:
        super().__init__(
            run
            or RipgrepRun(
                exit_code=0, stderr_output="", timed_out=False, timeout_seconds=30.0
            )
        )
        self._stdout = stdout

    async def run(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> RipgrepRun:
        Path(output_file_path).write_text(self._stdout)
        return await super().run(
            argv=argv,
            working_dir=working_dir,
            output_file_path=output_file_path,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )


class _LocalWritingBackend(_WritingBackend):
    @property
    def is_local(self) -> bool:
        return True


def _rg_match(path: Path, line: int, text: str) -> str:
    import json

    return json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": str(path)},
                "lines": {"text": text},
                "line_number": line,
                "submatches": [{"match": {"text": "needle"}, "start": 0, "end": 6}],
            },
        }
    )


def _missing(search_path: Path) -> RipgrepRun:
    return RipgrepRun(
        exit_code=2,
        stderr_output=f"{search_path}: No such file or directory (os error 2)\n",
        timed_out=False,
        timeout_seconds=30.0,
        is_error=True,
    )


@pytest.fixture
def index_state_only(tmp_path: Path) -> Path:
    repo = tmp_path / "golden-repos" / "agent-router"
    (repo / ".code-indexer").mkdir(parents=True)
    return repo.resolve()


@pytest.mark.asyncio
async def test_a_sub_path_this_pod_lacks_is_searched_where_the_repo_is(
    index_state_only: Path,
) -> None:
    hit = index_state_only / "src" / "router.py"
    backend = _WritingBackend(_rg_match(hit, 3, "needle()\n") + "\n")
    svc = RegexSearchService(index_state_only, ripgrep_backend=backend)

    result = await svc.search(pattern="needle", path="src", max_results=5)

    assert backend.calls[0]["argv"][-2:] == ["--", str(index_state_only / "src")]
    assert [(m.file_path, m.line_number) for m in result.matches] == [
        ("src/router.py", 3)
    ]


@pytest.mark.asyncio
async def test_a_path_missing_where_the_repo_is_is_still_a_caller_error(
    index_state_only: Path,
) -> None:
    backend = _WritingBackend(run=_missing(index_state_only / "nope"))
    svc = RegexSearchService(index_state_only, ripgrep_backend=backend)

    with pytest.raises(ValueError, match="Path does not exist: nope"):
        await svc.search(pattern="needle", path="nope", max_results=5)


@pytest.mark.asyncio
async def test_any_other_delegated_failure_is_still_a_ripgrep_error(
    index_state_only: Path,
) -> None:
    from code_indexer.global_repos.regex_search import RipgrepExecutionError

    other = index_state_only / "src" / "unreadable.py"
    backend = _WritingBackend(run=_missing(other))  # names a file, not the path
    svc = RegexSearchService(index_state_only, ripgrep_backend=backend)

    with pytest.raises(RipgrepExecutionError):
        await svc.search(pattern="needle", path="src", max_results=5)


@pytest.mark.asyncio
async def test_the_local_backend_keeps_the_local_existence_check(
    index_state_only: Path,
) -> None:
    backend = _LocalWritingBackend()
    svc = RegexSearchService(index_state_only, ripgrep_backend=backend)

    with pytest.raises(ValueError, match="Path does not exist: src"):
        await svc.search(pattern="needle", path="src", max_results=5)
    assert backend.calls == [], "ripgrep ran for a path the local check rejects"


@pytest.mark.asyncio
async def test_a_local_missing_path_message_is_not_reinterpreted(
    index_state_only: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locally the existence check owns that answer; an rg error naming the
    path (it vanished mid-search) stays a ripgrep error."""
    import code_indexer.global_repos.regex_search as mod
    from code_indexer.global_repos.regex_search import RipgrepExecutionError

    # A local backend picks its engine from PATH; pin ripgrep so this holds
    # where rg is not installed (the CI smoke image).
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}")

    (index_state_only / "src").mkdir()
    backend = _LocalWritingBackend(run=_missing(index_state_only / "src"))
    svc = RegexSearchService(index_state_only, ripgrep_backend=backend)

    with pytest.raises(RipgrepExecutionError):
        await svc.search(pattern="needle", path="src", max_results=5)


# --- the server hands its configured backend to every regex search ------------


def test_the_server_accessor_returns_the_backend_startup_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from code_indexer.server.startup import ripgrep_backend_wiring as wiring
    from code_indexer.server.utils import registry_factory

    backend = _RecordingBackend()
    monkeypatch.setattr(
        registry_factory,
        "_running_server_app_state",
        lambda: SimpleNamespace(ripgrep_backend=backend),
    )
    assert wiring.server_ripgrep_backend() is backend
    monkeypatch.setattr(registry_factory, "_running_server_app_state", lambda: None)
    assert wiring.server_ripgrep_backend() is None


class _Constructed(Exception):
    pass


def _record_construction(captured: dict):
    def _service(*args, **kwargs):
        captured.update(kwargs, root=args[0])
        raise _Constructed

    return _service


@pytest.mark.asyncio
async def test_the_rest_route_passes_the_servers_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.routes import regex_routes
    from code_indexer.server.startup import ripgrep_backend_wiring as wiring

    backend = _RecordingBackend()
    captured: dict = {}
    monkeypatch.setattr(wiring, "server_ripgrep_backend", lambda: backend)
    monkeypatch.setattr(
        regex_routes, "RegexSearchService", _record_construction(captured)
    )

    with pytest.raises(_Constructed):
        await regex_routes._execute_single_search(
            regex_routes.RegexSearchRequest(repository_alias="r", pattern="x"),
            str(tmp_path),
        )
    assert captured["ripgrep_backend"] is backend


@pytest.mark.asyncio
async def test_the_mcp_tool_passes_the_servers_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    import code_indexer.global_repos.regex_search as service_module
    from code_indexer.server.mcp.handlers.search import regex_search as handler
    from code_indexer.server.startup import ripgrep_backend_wiring as wiring

    backend = _RecordingBackend()
    captured: dict = {}
    monkeypatch.setattr(wiring, "server_ripgrep_backend", lambda: backend)
    monkeypatch.setattr(handler, "get_config_service", MagicMock())
    monkeypatch.setattr(
        service_module, "RegexSearchService", _record_construction(captured)
    )

    with pytest.raises(_Constructed):
        await handler._execute_regex_search_impl(
            {"pattern": "x"}, tmp_path, "r", MagicMock()
        )
    assert captured["ripgrep_backend"] is backend


# --- a delegated search runs where workspace-mcp can see the repository -------
#
# Refresh points an alias at a versioned snapshot on THIS pod's disk; the
# delegated backend's ripgrep runs in workspace-mcp, which holds only the
# golden repos' base clones.


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace

    from code_indexer.server.repositories import golden_repo_manager as grm

    base = tmp_path / "golden-repos"
    clones = {"agent-router": str(base / "agent-router")}
    manager = SimpleNamespace(
        get_golden_repo=lambda name: (
            SimpleNamespace(clone_path=clones[name]) if name in clones else None
        )
    )
    monkeypatch.setattr(grm, "get_golden_repo_manager", lambda: manager)
    return base


def _target(monkeypatch: pytest.MonkeyPatch, backend, repo_path: Path):
    from code_indexer.server.startup import ripgrep_backend_wiring as wiring

    monkeypatch.setattr(wiring, "server_ripgrep_backend", lambda: backend)
    return wiring.server_regex_target(repo_path)


def test_a_snapshot_is_searched_at_its_base_clone(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _RecordingBackend()
    snapshot = golden / ".versioned" / "agent-router" / "v_1760000000"

    assert _target(monkeypatch, backend, snapshot) == (
        golden / "agent-router",
        backend,
    )


def test_a_base_clone_path_is_unchanged(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _RecordingBackend()
    assert _target(monkeypatch, backend, golden / "agent-router") == (
        golden / "agent-router",
        backend,
    )


@pytest.mark.parametrize("backend", [None, "local"])
def test_a_local_search_keeps_the_snapshot(
    golden: Path, monkeypatch: pytest.MonkeyPatch, backend
) -> None:
    """Locally the snapshot IS the indexed checkout, and it is on this disk."""
    chosen = LocalRipgrepBackend() if backend == "local" else None
    snapshot = golden / ".versioned" / "agent-router" / "v_1760000000"
    assert _target(monkeypatch, chosen, snapshot) == (snapshot, chosen)


def test_an_unregistered_snapshot_is_left_alone(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = golden / ".versioned" / "gone" / "v_1760000000"
    assert _target(monkeypatch, _RecordingBackend(), snapshot)[0] == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["rest", "mcp"])
async def test_both_entry_points_search_the_base_clone(
    golden: Path, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    from unittest.mock import MagicMock

    import code_indexer.global_repos.regex_search as service_module
    from code_indexer.server.mcp.handlers.search import regex_search as handler
    from code_indexer.server.routes import regex_routes
    from code_indexer.server.startup import ripgrep_backend_wiring as wiring

    snapshot = golden / ".versioned" / "agent-router" / "v_1760000000"
    captured: dict = {}
    monkeypatch.setattr(wiring, "server_ripgrep_backend", _RecordingBackend)
    monkeypatch.setattr(handler, "get_config_service", MagicMock())
    for module in (regex_routes, service_module):
        monkeypatch.setattr(
            module, "RegexSearchService", _record_construction(captured)
        )

    with pytest.raises(_Constructed):
        if site == "rest":
            await regex_routes._execute_single_search(
                regex_routes.RegexSearchRequest(repository_alias="r", pattern="x"),
                str(snapshot),
            )
        else:
            await handler._execute_regex_search_impl(
                {"pattern": "x"}, snapshot, "agent-router-global", MagicMock()
            )
    assert captured["root"] == golden / "agent-router"
