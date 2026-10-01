"""Issue #1608: RegexSearchService._find_files_by_patterns() (the grep-fallback
glob path, exercised when ripgrep is unavailable and an include pattern
contains "/") has two production-scale defects:

1. search_path.exists() is called directly on the event-loop thread instead
   of being offloaded via anyio.to_thread.run_sync -- on a `hard` NFSv3
   mount this stat call can block the whole server event loop indefinitely
   (see this project's CLAUDE.md "Production Scale" invariant).
2. The glob subprocess's output is read with no max_output_bytes bound,
   unlike the bounded-read fix already applied elsewhere in this module for
   issue #1601 -- a pathological glob match set can grow the temp output
   file, and the eventual in-memory read/json.loads, without limit.

Both tests below are discriminating: they fail against the pre-fix code and
pass once the fix (anyio.to_thread.run_sync wrap + max_output_bytes on the
execute_with_limits call) is applied. Real filesystem + real subprocess
throughout. Neither test mocks the mechanism under test (Path.exists and the
glob subprocess both execute for real) -- the first test only observes what
is passed to anyio.to_thread.run_sync via a spy that transparently delegates
every call to the real implementation.
"""

from __future__ import annotations

import pathlib
import shutil
from unittest.mock import MagicMock, patch

import anyio.to_thread
import pytest

import code_indexer.global_repos.regex_search as regex_search_module
from code_indexer.global_repos.regex_search import RegexSearchService

_SYNTHETIC_FILE_COUNT = 3000
_FILENAME_PADDING_LENGTH = 30
_TEST_BYTE_CEILING = 30_000  # well under one 64 KiB pipe/read chunk's worth
_TEST_TIMEOUT_SECONDS = 30


def _build_grep_service(repo_path) -> RegexSearchService:
    """RegexSearchService pinned to the grep (non-ripgrep) fallback engine.

    Only "rg" is faked as unavailable; "grep" itself resolves through the
    real shutil.which so the test stays portable across machines/OSes
    instead of hardcoding an executable path.
    """
    real_which = shutil.which
    with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
        mock_which.side_effect = lambda cmd: None if cmd == "rg" else real_which(cmd)
        return RegexSearchService(repo_path)


class TestFindFilesByPatternsExistsOffload:
    """Defect 1: search_path.exists() must run off the event-loop thread."""

    @pytest.mark.asyncio
    async def test_search_path_exists_check_runs_off_event_loop_thread(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "file1.py").write_text("def func():\n    pass\n")
        service = _build_grep_service(tmp_path)

        # Spy on anyio.to_thread.run_sync: every call is forwarded, via
        # MagicMock(wraps=...), to the REAL implementation -- this never
        # replaces or short-circuits the actual offload/thread-pool
        # mechanism, it only records what gets submitted to it so the test
        # can prove search_path.exists was (or wasn't) one of those calls.
        real_run_sync = anyio.to_thread.run_sync
        spy_run_sync = MagicMock(wraps=real_run_sync)

        with patch(
            "code_indexer.global_repos.regex_search.anyio.to_thread.run_sync",
            spy_run_sync,
        ):
            await service._find_files_by_patterns(
                search_path=tmp_path,
                include_patterns=["sub/*.py"],
                exclude_patterns=None,
                timeout_seconds=_TEST_TIMEOUT_SECONDS,
            )

        offloaded_exists_calls = [
            call
            for call in spy_run_sync.call_args_list
            if call.args
            and getattr(call.args[0], "__func__", None) is pathlib.Path.exists
            and getattr(call.args[0], "__self__", None) == tmp_path
        ]
        assert offloaded_exists_calls, (
            "search_path.exists() was never submitted to "
            "anyio.to_thread.run_sync -- it is running directly on the "
            "event-loop (calling) thread instead of being offloaded"
        )


class TestFindFilesByPatternsOutputBound:
    """Defect 2: the glob subprocess's output must be bounded via
    max_output_bytes, mirroring the #1601 fix applied elsewhere in this
    module (e.g. the ripgrep/grep search paths' execute_with_limits calls)."""

    @pytest.fixture
    def many_files_repo(self, tmp_path):
        """A repo with enough files that the glob script's JSON output
        exceeds one 64 KiB pipe/read chunk -- large enough to force a real,
        multi-chunk subprocess read and prove genuine write-time capping
        (not just a single-read coincidence)."""
        repo_path = tmp_path / "many-files-repo"
        repo_path.mkdir()
        padding = "x" * _FILENAME_PADDING_LENGTH
        for i in range(_SYNTHETIC_FILE_COUNT):
            (repo_path / f"file_{i:05d}_padding_{padding}.py").write_text("x")
        return repo_path

    @pytest.mark.asyncio
    async def test_glob_output_read_is_bounded_not_unlimited(self, many_files_repo):
        service = _build_grep_service(many_files_repo)

        with patch.object(regex_search_module, "_MAX_READ_BYTES", _TEST_BYTE_CEILING):
            result = await service._find_files_by_patterns(
                search_path=many_files_repo,
                include_patterns=["**/*.py"],
                exclude_patterns=None,
                timeout_seconds=_TEST_TIMEOUT_SECONDS,
            )

        # Pre-fix: execute_with_limits is called with no max_output_bytes at
        # all, so the full JSON output -- well beyond the patched ceiling --
        # is written and read unbounded, and every one of the
        # _SYNTHETIC_FILE_COUNT files is returned.
        #
        # Post-fix: max_output_bytes=_MAX_READ_BYTES truncates the
        # subprocess's stdout at write-time, corrupting the JSON mid-array;
        # the existing json.JSONDecodeError handler then returns [] rather
        # than the full list. Either way, "far fewer than the full set"
        # proves the read is now bounded.
        assert len(result) < _SYNTHETIC_FILE_COUNT, (
            "glob output was read/parsed in full despite exceeding the "
            "patched _MAX_READ_BYTES ceiling -- max_output_bytes is not "
            "being passed to execute_with_limits"
        )
