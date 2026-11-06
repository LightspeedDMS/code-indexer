"""Regex search walks only what it needs and stops reading once it has enough.

* The literal directory every include pattern lies under becomes ripgrep's
  search root, so ``src/app/**/*.java`` no longer walks the whole repository.
  A narrowed root that is absent (or leaves the repository) means no matches,
  never "Path does not exist" -- the caller named no such path.
* A backend whose ``run`` takes ``stop_after_matches`` is told to stop after
  ``max_results + 1`` matches; a run it stopped early is a successful,
  truncated search -- never an error, never read-capped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional, Sequence

import pytest

from code_indexer.global_repos.regex_search import (
    RegexSearchService,
    literal_include_root,
)
from code_indexer.server.storage.shared.ripgrep_backend import (
    RipgrepRun,
    accepts_stop_after_matches,
    local_ripgrep_backend,
)

ROOT = Path("/golden-repos/example-repo")


def _ok(**kw: Any) -> RipgrepRun:
    defaults: dict = dict(
        exit_code=0, stderr_output="", timed_out=False, timeout_seconds=30.0
    )
    defaults.update(kw)
    return RipgrepRun(**defaults)


def _match(path: str, line: int) -> str:
    return json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": path},
                "lines": {"text": "needle()\n"},
                "line_number": line,
                "submatches": [{"match": {"text": "needle"}, "start": 0, "end": 6}],
            },
        }
    )


class _Backend:
    """A delegated backend predating ``stop_after_matches``."""

    def __init__(self, stdout: str = "", result: Optional[RipgrepRun] = None):
        self.stdout = stdout
        self.result = result or _ok()
        self.argvs: List[List[str]] = []
        self.local = False

    @property
    def is_local(self) -> bool:
        return self.local

    async def run(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> RipgrepRun:
        self.argvs.append(list(argv))
        Path(output_file_path).write_text(self.stdout)
        return self.result

    async def supports_pcre2(self) -> bool:
        return False


class _StoppingBackend(_Backend):
    """A backend that honours ``stop_after_matches``."""

    def __init__(self, stdout: str = "", result: Optional[RipgrepRun] = None):
        super().__init__(stdout, result)
        self.stops: List[Optional[int]] = []

    async def run(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: float,
        max_output_bytes: int,
        stop_after_matches: Optional[int] = None,
    ) -> RipgrepRun:
        self.stops.append(stop_after_matches)
        return await super().run(
            argv=argv,
            working_dir=working_dir,
            output_file_path=output_file_path,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )


def _paths(argv: List[str]) -> List[str]:
    return argv[argv.index("--") + 1 :]


@pytest.fixture(autouse=True)
def _no_trigram(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_indexer.global_repos import trigram_index_manager

    monkeypatch.setattr(
        trigram_index_manager, "trigram_prefilter_available", lambda: False
    )


# ── the literal include root ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "patterns, expected",
    [
        (["code/src/app/logic/*.java"], "code/src/app/logic"),
        (["code/src/app/**/*.java"], "code/src/app"),
        (["code/src/a/*.java", "code/src/b/*.java"], "code/src"),
        (["./code/src/*.py"], "code/src"),
        (["src/main/"], "src"),
        (["src/Foo.java"], "src"),
        (["*.java"], None),
        (["**/x.java"], None),
        (["Makefile"], None),
        (["src/*.py", "*.md"], None),
        (["a/*.py", "b/*.py"], None),
        (["{a,b}/x.py"], None),
        ([], None),
        (None, None),
    ],
)
def test_literal_include_root(
    patterns: Optional[List[str]], expected: Optional[str]
) -> None:
    assert literal_include_root(patterns) == expected


@pytest.mark.asyncio
async def test_ripgrep_starts_at_the_include_root() -> None:
    backend = _Backend(_match(f"{ROOT}/code/src/app/A.java", 3) + "\n")
    svc = RegexSearchService(ROOT, ripgrep_backend=backend)

    result = await svc.search(
        pattern="needle", include_patterns=["code/src/app/**/*.java"]
    )

    assert _paths(backend.argvs[0]) == [f"{ROOT}/code/src/app"]
    # The globs still apply, unchanged, to repo-relative paths.
    assert "code/src/app/**/*.java" in backend.argvs[0]
    assert [m.file_path for m in result.matches] == ["code/src/app/A.java"]


@pytest.mark.asyncio
async def test_an_explicit_path_inside_the_include_root_wins() -> None:
    backend = _Backend()
    svc = RegexSearchService(ROOT, ripgrep_backend=backend)

    await svc.search(
        pattern="needle", path="code/src/app/x", include_patterns=["code/**/*.java"]
    )

    assert _paths(backend.argvs[0]) == [f"{ROOT}/code/src/app/x"]


@pytest.mark.asyncio
async def test_an_include_root_outside_the_path_is_not_used() -> None:
    backend = _Backend()
    svc = RegexSearchService(ROOT, ripgrep_backend=backend)

    await svc.search(pattern="needle", path="docs", include_patterns=["code/*.java"])

    assert _paths(backend.argvs[0]) == [f"{ROOT}/docs"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stderr",
    [
        f"rg: {ROOT}/nope/dir: IO error for operation on {ROOT}/nope/dir: "
        "No such file or directory (os error 2)",
        "delegated ripgrep failed: /content/rg returned 403: path escapes "
        f"working_dir: {ROOT}/nope/dir",
    ],
)
async def test_an_absent_include_root_is_no_matches(stderr: str) -> None:
    backend = _Backend("", _ok(exit_code=2, stderr_output=stderr, is_error=True))
    svc = RegexSearchService(ROOT, ripgrep_backend=backend)

    result = await svc.search(pattern="needle", include_patterns=["nope/dir/*.py"])

    assert result.matches == [] and result.total_matches == 0


@pytest.mark.asyncio
async def test_other_errors_on_a_narrowed_root_still_raise() -> None:
    from code_indexer.global_repos.regex_search import RipgrepExecutionError

    backend = _Backend(
        "", _ok(exit_code=2, stderr_output="regex parse error", is_error=True)
    )
    svc = RegexSearchService(ROOT, ripgrep_backend=backend)

    with pytest.raises(RipgrepExecutionError):
        await svc.search(pattern="needle", include_patterns=["src/dir/*.py"])


def test_a_local_root_through_a_symlink_is_not_used(tmp_path: Path) -> None:
    (tmp_path / "real" / "pkg").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    backend = _Backend()
    svc = RegexSearchService(tmp_path, ripgrep_backend=backend)
    backend.local = True

    assert svc._narrowed_search_root(tmp_path, ["link/pkg/*.py"]) == tmp_path
    assert (
        svc._narrowed_search_root(tmp_path, ["real/pkg/*.py"])
        == tmp_path / "real" / "pkg"
    )


def test_a_root_inside_git_is_never_used() -> None:
    svc = RegexSearchService(ROOT, ripgrep_backend=_Backend())
    assert svc._narrowed_search_root(ROOT, [".git/hooks/*"]) == ROOT


# ── stopping early ─────────────────────────────────────────────────────────


def test_accepts_stop_after_matches_reads_the_signature() -> None:
    assert accepts_stop_after_matches(_StoppingBackend()) is True
    assert accepts_stop_after_matches(_Backend()) is False
    assert accepts_stop_after_matches(local_ripgrep_backend()) is False
    assert accepts_stop_after_matches(object()) is False


@pytest.mark.asyncio
async def test_the_limit_is_sent_only_to_a_backend_that_takes_it() -> None:
    stopping, plain = _StoppingBackend(), _Backend()

    await RegexSearchService(ROOT, ripgrep_backend=stopping).search(
        pattern="needle", max_results=7
    )
    await RegexSearchService(ROOT, ripgrep_backend=plain).search(
        pattern="needle", max_results=7
    )

    assert stopping.stops == [8]
    assert len(plain.argvs) == 1


@pytest.mark.asyncio
async def test_a_run_stopped_early_is_a_truncated_success() -> None:
    stdout = "\n".join(_match(f"{ROOT}/f{i}.py", 1) for i in range(3)) + "\n"
    backend = _StoppingBackend(
        stdout, _ok(exit_code=None, is_error=True, stopped_early=True)
    )

    result = await RegexSearchService(ROOT, ripgrep_backend=backend).search(
        pattern="needle", max_results=2
    )

    assert len(result.matches) == 2
    assert result.truncated is True
    assert result.read_capped is False


@pytest.mark.asyncio
async def test_a_run_stopped_at_exactly_max_results_is_still_truncated() -> None:
    """The count alone cannot tell: the backend stopped with exactly
    max_results matches on disk, so ``truncated`` must come from
    ``stopped_early``."""
    stdout = "\n".join(_match(f"{ROOT}/f{i}.py", 1) for i in range(3)) + "\n"
    backend = _StoppingBackend(
        stdout, _ok(exit_code=None, is_error=True, stopped_early=True)
    )
    svc = RegexSearchService(ROOT, ripgrep_backend=backend)

    result = await svc.search(pattern="needle", max_results=3)

    assert len(result.matches) == 3
    assert result.truncated is True
    assert result.read_capped is False

    # A later complete search on the same service is not marked truncated.
    backend.result = _ok()
    again = await svc.search(pattern="needle", max_results=3)
    assert again.truncated is False


# ── stopping a delegated run by cancelling it ───────────────────────────────


class _SlowBackend(_Backend):
    """The workspace-mcp plugin's shape: no ``stop_after_matches``; writes
    match lines a few at a time and stops when its task is cancelled."""

    def __init__(self, lines: List[str], tail: str = "", run_ok: bool = True):
        super().__init__()
        self.lines = lines
        self.tail = tail
        self.cancelled = False
        self.written = 0
        self.run_ok = run_ok

    async def run(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: float,
        max_output_bytes: int,
        threads: Optional[int] = None,
    ) -> RipgrepRun:
        import asyncio

        self.argvs.append(list(argv))
        try:
            with open(output_file_path, "w") as out:
                for line in self.lines:
                    out.write(line + "\n")
                    out.flush()
                    self.written += 1
                    await asyncio.sleep(0.01)
                out.write(self.tail)  # a partial trailing line
                out.flush()
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return _ok()


@pytest.mark.asyncio
async def test_a_delegated_run_is_cancelled_after_max_results_plus_one() -> None:
    lines = [_match(f"{ROOT}/f{i}.py", 1) for i in range(40)]
    backend = _SlowBackend(lines, tail='{"type":"match","data":{"pa')

    result = await RegexSearchService(ROOT, ripgrep_backend=backend).search(
        pattern="needle", max_results=3
    )

    assert backend.cancelled is True
    assert backend.written < len(lines)
    assert [m.file_path for m in result.matches] == ["f0.py", "f1.py", "f2.py"]
    assert result.truncated is True
    assert result.read_capped is False


@pytest.mark.asyncio
async def test_a_partial_trailing_line_is_not_counted(tmp_path: Path) -> None:
    from code_indexer.global_repos.regex_search import _MatchLineCounter

    out = tmp_path / "out.jsonl"
    out.write_text(_match("a", 1) + "\n" + '{"type":"match","data"')
    counter = _MatchLineCounter(str(out))
    assert counter.advance(2) is None and counter.matches == 1
    with open(out, "a") as fh:
        fh.write(":{}}\n")
    assert counter.advance(2) == out.stat().st_size
    assert counter.matches == 2


@pytest.mark.asyncio
async def test_a_run_that_finishes_first_is_returned_unchanged() -> None:
    lines = [_match(f"{ROOT}/f{i}.py", 1) for i in range(2)]

    class _Quick(_SlowBackend):
        async def run(
            self,
            *,
            argv: Sequence[str],
            working_dir: str,
            output_file_path: str,
            timeout_seconds: float,
            max_output_bytes: int,
            threads: Optional[int] = None,
        ) -> RipgrepRun:
            Path(output_file_path).write_text("\n".join(lines) + "\n")
            return _ok()

    backend = _Quick(lines)
    result = await RegexSearchService(ROOT, ripgrep_backend=backend).search(
        pattern="needle", max_results=5
    )

    assert backend.cancelled is False
    assert len(result.matches) == 2 and result.truncated is False


@pytest.mark.asyncio
async def test_cancelling_the_search_cancels_the_run() -> None:
    import asyncio

    backend = _SlowBackend([_match(f"{ROOT}/a.py", 1)])
    svc = RegexSearchService(ROOT, ripgrep_backend=backend)
    search = asyncio.ensure_future(svc.search(pattern="needle", max_results=50))
    await asyncio.sleep(0.3)
    search.cancel()
    with pytest.raises(asyncio.CancelledError):
        await search
    await asyncio.sleep(0)
    assert backend.cancelled is True


@pytest.mark.asyncio
async def test_a_backend_timeout_is_still_a_timeout() -> None:
    class _TimesOut(_SlowBackend):
        async def run(
            self,
            *,
            argv: Sequence[str],
            working_dir: str,
            output_file_path: str,
            timeout_seconds: float,
            max_output_bytes: int,
            threads: Optional[int] = None,
        ) -> RipgrepRun:
            Path(output_file_path).write_text("")
            return _ok(exit_code=None, timed_out=True)

    with pytest.raises(TimeoutError):
        await RegexSearchService(ROOT, ripgrep_backend=_TimesOut([])).search(
            pattern="needle", max_results=5
        )
