"""Bug #1608 code-review follow-up (3 items, all confined to
``RegexSearchService._find_files_by_patterns()`` in
``src/code_indexer/global_repos/regex_search.py``):

F1 (Medium): the read+parse of the (now-bounded) glob subprocess output
still runs directly on the event-loop thread, unlike both sibling call
sites (``_read_and_parse_ripgrep`` / ``_read_and_parse_grep``), which
offload their equivalent phase via ``anyio.to_thread.run_sync``.

F2 (Low): a second un-offloaded ``.exists()`` check (``script_path.exists()``)
remains in the same method, ~25 lines below the one #1608 already fixed
(``search_path.exists()``).

F3 (Low/Medium): ``result.output_capped`` is dropped on the floor. The glob
child process emits ONE atomic JSON document (a single
``print(json.dumps(files))`` in ``scripts/glob_files.py``). When
``max_output_bytes`` (added by #1608's own fix) truncates that document
mid-write, ``json.loads`` necessarily raises -- the pre-fix code logs a
generic "Failed to parse glob output as JSON" warning and returns ``[]``,
which the caller turns into a silent "zero matches" result. Both sibling
call sites (``_search_ripgrep`` / ``_search_grep``) instead consume
``result.output_capped`` to set ``self._last_search_read_capped``, feeding
the existing AC-I3 fleet-scale observability warning in ``search()``.

All three tests below are discriminating: real filesystem, real
subprocess, real ``glob_files.py`` script execution throughout -- no
mocking of the mechanism under test. Each fails against the pre-fix code
and passes once the corresponding fix lands.
"""

from __future__ import annotations

import logging
import pathlib
import shutil
import threading
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


class TestFindFilesByPatternsReadParseOffload:
    """F1: the glob output read+parse must run off the event-loop thread."""

    @pytest.mark.asyncio
    async def test_read_and_parse_glob_output_runs_off_event_loop_thread(
        self, tmp_path
    ):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "file1.py").write_text("def func():\n    pass\n")
        service = _build_grep_service(tmp_path)

        event_loop_thread_id = threading.get_ident()
        recorded_thread_ids = []

        real_json_loads = regex_search_module.json.loads

        def spying_loads(*args, **kwargs):
            recorded_thread_ids.append(threading.get_ident())
            return real_json_loads(*args, **kwargs)

        with patch.object(regex_search_module.json, "loads", spying_loads):
            result = await service._find_files_by_patterns(
                search_path=tmp_path,
                include_patterns=["sub/*.py"],
                exclude_patterns=None,
                timeout_seconds=_TEST_TIMEOUT_SECONDS,
            )

        assert result == ["sub/file1.py"]
        assert recorded_thread_ids, "json.loads was never called"
        assert event_loop_thread_id not in recorded_thread_ids, (
            "glob output read+parse (json.loads) ran directly on the "
            "event-loop thread instead of being offloaded via "
            "anyio.to_thread.run_sync, unlike the sibling "
            "_read_and_parse_ripgrep/_read_and_parse_grep call sites"
        )


class TestFindFilesByPatternsScriptExistsOffload:
    """F2: the second .exists() check (script_path.exists()) must also be
    offloaded, mirroring the search_path.exists() fix #1608 already
    applied ~25 lines above it in the same method."""

    @pytest.mark.asyncio
    async def test_script_path_exists_check_runs_off_event_loop_thread(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "file1.py").write_text("def func():\n    pass\n")
        service = _build_grep_service(tmp_path)

        expected_script_path = (
            pathlib.Path(regex_search_module.__file__).parent.parent.parent.parent
            / "scripts"
            / "glob_files.py"
        )

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

        offloaded_script_exists_calls = [
            call
            for call in spy_run_sync.call_args_list
            if call.args
            and getattr(call.args[0], "__func__", None) is pathlib.Path.exists
            and getattr(call.args[0], "__self__", None) == expected_script_path
        ]
        assert offloaded_script_exists_calls, (
            "script_path.exists() was never submitted to "
            "anyio.to_thread.run_sync -- it is running directly on the "
            "event-loop (calling) thread instead of being offloaded"
        )


class TestFindFilesByPatternsOutputCappedSignaling:
    """F3: a capped (truncated) glob subprocess output must be diagnosed
    as a capacity limit via self._last_search_read_capped, not silently
    reported as "no matches" behind a misleading JSON-parse-error log."""

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
    async def test_capped_glob_output_sets_last_search_read_capped(
        self, many_files_repo, caplog
    ):
        service = _build_grep_service(many_files_repo)

        with patch.object(regex_search_module, "_MAX_READ_BYTES", _TEST_BYTE_CEILING):
            with caplog.at_level(logging.WARNING):
                result = await service._find_files_by_patterns(
                    search_path=many_files_repo,
                    include_patterns=["**/*.py"],
                    exclude_patterns=None,
                    timeout_seconds=_TEST_TIMEOUT_SECONDS,
                )

        # Truncation corrupts the single atomic JSON document, so the
        # result is still an empty list either way -- what must change is
        # the DIAGNOSIS, not the (already-correct) graceful-degradation
        # return value.
        assert result == []

        assert service._last_search_read_capped is True, (
            "glob output was truncated by max_output_bytes but "
            "_last_search_read_capped was never set -- a real capacity "
            "limit at ~900-repo fleet scale is being silently reported "
            "as 'no matches' instead of surfacing through the existing "
            "AC-I3 read-capped signal (mirrors _search_ripgrep/"
            "_search_grep's handling of result.output_capped)"
        )

        misleading_messages = [
            record.message
            for record in caplog.records
            if "Failed to parse glob output as JSON" in record.message
        ]
        assert not misleading_messages, (
            "capped glob output logged the generic/misleading "
            f"parse-error message instead of being diagnosed as a "
            f"capacity limit: {misleading_messages}"
        )
