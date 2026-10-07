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
