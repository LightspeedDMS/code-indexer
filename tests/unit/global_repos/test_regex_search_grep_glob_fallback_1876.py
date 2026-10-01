"""Bug #1876: grep fallback uses the canonical path selector for globs."""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from unittest.mock import patch

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService


def _grep_only_service(repo_path) -> RegexSearchService:
    """Build the real grep fallback while making only ripgrep unavailable."""
    real_which = shutil.which
    with patch("code_indexer.global_repos.regex_search.shutil.which") as which:
        which.side_effect = lambda command: (
            None if command == "rg" else real_which(command)
        )
        return RegexSearchService(repo_path)


@pytest.mark.asyncio
async def test_grep_fallback_normalizes_any_depth_globs(tmp_path):
    """``*/tests/*`` has the same any-depth policy as every other engine."""
    for relative_path in (
        "tests/root.py",
        "src/tests/direct.py",
        "x/y/tests/nested.py",
    ):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("FallbackNeedle\n")
    (tmp_path / "src" / "main.py").write_text("FallbackNeedle\n")

    matches, _ = await _grep_only_service(tmp_path)._search_grep(
        pattern="FallbackNeedle",
        search_path=tmp_path,
        include_patterns=["*/tests/*"],
        exclude_patterns=None,
        case_sensitive=True,
        context_lines=0,
        max_results=20,
        timeout_seconds=30,
    )

    assert {match.file_path for match in matches} == {
        "tests/root.py",
        "src/tests/direct.py",
        "x/y/tests/nested.py",
    }


@pytest.mark.asyncio
async def test_grep_fallback_applies_excludes_without_an_include(tmp_path):
    """Bare exclude-only searches do not delegate glob semantics to grep."""
    (tmp_path / "README.md").write_text("FallbackNeedle\n")
    (tmp_path / "Service.java").write_text("FallbackNeedle\n")

    matches, _ = await _grep_only_service(tmp_path)._search_grep(
        pattern="FallbackNeedle",
        search_path=tmp_path,
        include_patterns=None,
        exclude_patterns=["*.md"],
        case_sensitive=True,
        context_lines=0,
        max_results=20,
        timeout_seconds=30,
    )

    assert {match.file_path for match in matches} == {"Service.java"}


@pytest.mark.asyncio
async def test_grep_fallback_with_narrowed_path_finds_nested_files_n2(tmp_path):
    """Bug #1876 N2 regression: ``path`` narrows ``search_path`` below the
    repo root (mirrors ``search()``'s own ``search_path = repo_path / path``
    at regex_search.py:~771), and the glob script returns file paths
    relative to THAT narrowed search_path -- but the grep subprocess is
    always invoked with ``working_dir=str(self.repo_path)`` (the repo
    root). Passing the narrowed-relative paths straight through as grep's
    file arguments makes grep look for e.g. "a.ts" at the repo root, where
    it does not exist, and grep exits 2 with "No such file or directory"
    instead of returning the match. HEAD (pre-regression) returned both
    files for this exact scenario.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("FallbackNeedle\n")
    (tmp_path / "src" / "a").mkdir()
    (tmp_path / "src" / "a" / "b.ts").write_text("FallbackNeedle\n")

    matches, _ = await _grep_only_service(tmp_path)._search_grep(
        pattern="FallbackNeedle",
        search_path=tmp_path / "src",
        include_patterns=["*.ts"],
        exclude_patterns=None,
        case_sensitive=True,
        context_lines=0,
        max_results=20,
        timeout_seconds=30,
    )

    assert {match.file_path for match in matches} == {"src/a.ts", "src/a/b.ts"}


@pytest.mark.asyncio
async def test_grep_fallback_exclude_only_skips_internal_directories_n2(tmp_path):
    """Bug #1876 N2 regression: an exclude-only call widens the include to
    ``["**/*"]`` (regex_search.py:~2204) and the glob script's plain
    ``rglob("*")`` walk (scripts/glob_files.py) has no awareness of CIDX's
    own internal directories, unlike the ripgrep engine (which always
    appends ``-g '!.code-indexer/**'``/``-g '!.git/**'``, Bug #158) and
    unlike this same method's "no filters" branch (explicit
    ``--exclude-dir``). Real content placed inside ``.git`` and
    ``.code-indexer`` must never surface in results.
    """
    (tmp_path / "README.md").write_text("FallbackNeedle\n")
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "blob").write_text("FallbackNeedle\n")
    (tmp_path / ".code-indexer").mkdir()
    (tmp_path / ".code-indexer" / "internal.md").write_text("FallbackNeedle\n")

    matches, _ = await _grep_only_service(tmp_path)._search_grep(
        pattern="FallbackNeedle",
        search_path=tmp_path,
        include_patterns=None,
        exclude_patterns=["*.ts"],
        case_sensitive=True,
        context_lines=0,
        max_results=20,
        timeout_seconds=30,
    )

    assert {match.file_path for match in matches} == {"README.md"}


@pytest.mark.asyncio
async def test_grep_fallback_batches_large_file_lists_n2(tmp_path, monkeypatch):
    """Bug #1876 N2: the whole matched-file list must never be handed to
    grep on a single command line unbounded -- a large repository risks
    "argument list too long". Force a tiny batch size so a file list far
    larger than one batch is exercised, and prove every file across
    multiple grep invocations is still found and correctly attributed.
    """
    import code_indexer.global_repos.regex_search as regex_search_module

    monkeypatch.setattr(regex_search_module, "_GREP_FILE_LIST_BATCH_SIZE", 3)

    expected_files = set()
    for i in range(10):
        name = f"file_{i:02d}.py"
        (tmp_path / name).write_text("FallbackNeedle\n")
        expected_files.add(name)

    matches, total = await _grep_only_service(tmp_path)._search_grep(
        pattern="FallbackNeedle",
        search_path=tmp_path,
        include_patterns=["*.py"],
        exclude_patterns=None,
        case_sensitive=True,
        context_lines=0,
        max_results=100,
        timeout_seconds=30,
    )

    assert {match.file_path for match in matches} == expected_files
    assert total == 10


@pytest.mark.asyncio
async def test_grep_fallback_with_narrowed_path_matches_patterns_repo_relative_f4(
    tmp_path,
):
    """Bug #1876 round-5 finding F4: scripts/glob_files.py matched
    include/exclude patterns against paths relative to ``search_path``
    (the NARROWED directory ``path`` selects), while the indexed
    matcher, the Python multiline path, and real ripgrep all match
    against REPO-relative paths. With ``path="src"`` narrowing
    ``search_path`` to ``<repo>/src``:

    - ``include_patterns=["a/*.ts"]`` must match NOTHING -- real
      ``rg -g 'a/*.ts' -- src`` never matches ``src/a/b.ts`` (the
      pattern has no leading "src/"), but the old search_path-relative
      matching treated "a/b.ts" (relative to src/) as a match.
    - ``include_patterns=["**/*.ts"]`` + ``exclude_patterns=["a/**"]``
      must still return ``src/a/b.ts`` -- the exclude pattern
      "a/**" does not match the repo-relative path "src/a/b.ts" either.
    - ``include_patterns=["src/*.ts"]`` must return ``src/x.ts`` -- the
      pattern IS anchored with the repo-relative "src/" prefix.
    """
    (tmp_path / "src" / "a").mkdir(parents=True)
    (tmp_path / "src" / "a" / "b.ts").write_text("FallbackNeedle\n")
    (tmp_path / "src" / "x.ts").write_text("FallbackNeedle\n")

    service = _grep_only_service(tmp_path)

    matches, _ = await service._search_grep(
        pattern="FallbackNeedle",
        search_path=tmp_path / "src",
        include_patterns=["a/*.ts"],
        exclude_patterns=None,
        case_sensitive=True,
        context_lines=0,
        max_results=20,
        timeout_seconds=30,
    )
    assert {match.file_path for match in matches} == set(), (
        "include_patterns=['a/*.ts'] under path='src' must match nothing "
        "-- the pattern is repo-relative and has no leading 'src/'"
    )

    matches, _ = await service._search_grep(
        pattern="FallbackNeedle",
        search_path=tmp_path / "src",
        include_patterns=["**/*.ts"],
        exclude_patterns=["a/**"],
        case_sensitive=True,
        context_lines=0,
        max_results=20,
        timeout_seconds=30,
    )
    assert {match.file_path for match in matches} == {"src/a/b.ts", "src/x.ts"}, (
        "exclude_patterns=['a/**'] under path='src' must NOT drop "
        "src/a/b.ts -- 'a/**' does not match the repo-relative path"
    )

    matches, _ = await service._search_grep(
        pattern="FallbackNeedle",
        search_path=tmp_path / "src",
        include_patterns=["src/*.ts"],
        exclude_patterns=None,
        case_sensitive=True,
        context_lines=0,
        max_results=20,
        timeout_seconds=30,
    )
    assert {match.file_path for match in matches} == {"src/x.ts"}, (
        "include_patterns=['src/*.ts'] under path='src' must match "
        "src/x.ts via its repo-relative 'src/' anchor"
    )


_F5_BATCH_SIZE = 3
_F5_FILE_COUNT = 7  # -> 3 batches of size 3, 3, 1
_F5_EXPECTED_TOTAL_BATCHES = 3
_F5_CONTEXT_LINES = 0
_F5_MAX_RESULTS = 1000
_F5_PER_BATCH_DELAY_SECONDS = 0.6
_F5_TIMEOUT_SECONDS = 1
_F5_GREP_NO_MATCHES_EXIT_CODE = 1  # grep's own "no matches found" exit code
_F5_MAX_TOLERABLE_ELAPSED_SECONDS = (
    _F5_PER_BATCH_DELAY_SECONDS * _F5_EXPECTED_TOTAL_BATCHES
)  # what the pre-fix bug would take running all batches to completion


@pytest.mark.asyncio
async def test_grep_fallback_batch_loop_respects_shared_deadline_f5(
    tmp_path, monkeypatch
):
    """Bug #1876 round-5 finding F5: the glob-filtered batch loop in
    ``_search_grep`` applied the FULL ``timeout_seconds`` to every batch
    invocation, instead of passing the REMAINING deadline -- a repo with
    enough matched files to span several batches could run for
    ``num_batches * timeout_seconds`` wall-clock time even though the
    caller asked for a single bounded ``timeout_seconds`` budget overall.

    Reproduced by patching the external subprocess-execution boundary
    (``SubprocessExecutor.execute_with_limits``) to simulate a slow
    per-batch grep invocation and forcing multiple batches via a tiny
    ``_GREP_FILE_LIST_BATCH_SIZE``, with an overall
    ``timeout_seconds=_F5_TIMEOUT_SECONDS``. Before the fix: every batch
    runs to completion regardless of elapsed time (no TimeoutError, total
    elapsed approaches ``_F5_MAX_TOLERABLE_ELAPSED_SECONDS`` -- the full
    per-batch delay repeated across all ``_F5_EXPECTED_TOTAL_BATCHES``
    batches). After the fix: the exhausted remaining deadline is
    detected before the final batch would run, raising TimeoutError --
    mirroring how the single-invocation (no-glob-filter) path reports a
    timeout -- so fewer than ``_F5_EXPECTED_TOTAL_BATCHES`` batches
    execute and elapsed stays below that pre-fix ceiling.
    """
    import code_indexer.global_repos.regex_search as regex_search_module
    from code_indexer.server.services.subprocess_executor import (
        ExecutionStatus,
        SearchExecutionResult,
        SubprocessExecutor,
    )

    monkeypatch.setattr(
        regex_search_module, "_GREP_FILE_LIST_BATCH_SIZE", _F5_BATCH_SIZE
    )

    for i in range(_F5_FILE_COUNT):
        (tmp_path / f"file_{i:02d}.py").write_text("FallbackNeedle\n")

    call_count = 0
    real_execute_with_limits = SubprocessExecutor.execute_with_limits

    async def _slow_execute_with_limits(self, **kwargs):
        # Only intercept the real "grep" subprocess batches -- the
        # glob_files.py file-discovery subprocess (which runs first, to
        # build the candidate file list) must execute for real so the
        # batch loop this test targets is actually reached.
        if kwargs["command"][0] != "grep":
            return await real_execute_with_limits(self, **kwargs)
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(_F5_PER_BATCH_DELAY_SECONDS)
        # Clean "no matches" (grep exit code 1, no stderr) -- the output
        # file stays empty, which _read_and_parse_grep parses as ([], 0).
        return SearchExecutionResult(
            status=ExecutionStatus.ERROR,
            output_file=kwargs["output_file_path"],
            exit_code=_F5_GREP_NO_MATCHES_EXIT_CODE,
            timed_out=False,
        )

    monkeypatch.setattr(
        SubprocessExecutor, "execute_with_limits", _slow_execute_with_limits
    )

    service = _grep_only_service(tmp_path)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await service._search_grep(
            pattern="FallbackNeedle",
            search_path=tmp_path,
            include_patterns=["*.py"],
            exclude_patterns=None,
            case_sensitive=True,
            context_lines=_F5_CONTEXT_LINES,
            max_results=_F5_MAX_RESULTS,
            timeout_seconds=_F5_TIMEOUT_SECONDS,
        )
    elapsed = time.monotonic() - start

    assert call_count < _F5_EXPECTED_TOTAL_BATCHES, (
        f"batch loop ran all {call_count} batches despite an exhausted "
        f"shared deadline -- it must stop before the final batch"
    )
    assert elapsed < _F5_MAX_TOLERABLE_ELAPSED_SECONDS, (
        f"batch loop took {elapsed:.2f}s -- it must stop once the shared "
        f"{_F5_TIMEOUT_SECONDS}s deadline is exhausted, not run every "
        f"batch to completion (pre-fix ceiling: "
        f"{_F5_MAX_TOLERABLE_ELAPSED_SECONDS}s)"
    )


@pytest.mark.asyncio
async def test_find_files_by_patterns_launches_glob_script_with_sys_executable(
    tmp_path,
):
    """GitHub #1876 regression: the glob-filtered file-discovery subprocess
    must launch ``scripts/glob_files.py`` with ``sys.executable`` -- the
    SAME interpreter running the server -- not a bare ``"python3"`` looked
    up on PATH. ``glob_files.py`` imports ``code_indexer.services``
    (pathspec etc.); if the PATH-resolved ``python3`` is not the server's
    own interpreter (e.g. a bare system Python without the project's
    dependencies installed), every filtered grep-fallback search fails
    with "glob_files.py failed" even though HEAD (pre-regression, when the
    script was stdlib-only) worked under the same PATH.
    """
    from code_indexer.server.services.subprocess_executor import (
        ExecutionStatus,
        SearchExecutionResult,
        SubprocessExecutor,
    )

    (tmp_path / "a.py").write_text("content\n")

    captured_command = None

    async def _capture_command(self, **kwargs):
        nonlocal captured_command
        captured_command = kwargs["command"]
        # Real empty-list output so the caller parses cleanly.
        with open(kwargs["output_file_path"], "w") as f:
            f.write("[]")
        return SearchExecutionResult(
            status=ExecutionStatus.SUCCESS,
            output_file=kwargs["output_file_path"],
            exit_code=0,
        )

    with patch.object(SubprocessExecutor, "execute_with_limits", _capture_command):
        service = RegexSearchService(tmp_path)
        await service._find_files_by_patterns(
            search_path=tmp_path,
            include_patterns=["*.py"],
            exclude_patterns=None,
            timeout_seconds=30,
        )

    assert captured_command is not None, "glob subprocess was never invoked"
    assert captured_command[0] == sys.executable, (
        f"glob_files.py must be launched with sys.executable "
        f"({sys.executable!r}) -- the server's own interpreter -- not "
        f"{captured_command[0]!r}, since glob_files.py now imports "
        f"code_indexer.services and a mismatched PATH python3 may lack "
        f"that package"
    )
    assert captured_command[1].endswith("glob_files.py")
