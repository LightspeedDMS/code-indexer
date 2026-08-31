"""Issue #1601 remediation round 4, Priority 3 (Codex High finding,
investigated and confirmed real).

Claude's round-3 review confirmed ONLY that the three read+parse methods
(``_search_ripgrep``, ``_search_grep``, ``_search_python_multiline``) are
correctly offloaded via ``anyio.to_thread.run_sync`` -- it did not check
whether OTHER synchronous calls in ``search()``'s own pre-dispatch setup
remained un-offloaded. Codex's round-3 review flagged exactly that gap.

Investigation (this round) confirmed it was real:

- ``search_path.exists()`` is a filesystem stat call. Per this project's
  own Production Scale invariant, a `hard` NFSv3 mount can leave a stat
  blocked in UNINTERRUPTIBLE kernel retry FOREVER against an unresponsive
  NFS host -- exactly the class of call the invariant exists to prevent
  from running inside ``async def``.
- ``self._detect_pcre2_support()`` runs a real ``subprocess.run()``
  fork+exec on its first call per process.
- ``self._prefilter_candidate_files()`` opens a real ``sqlite3``
  connection (via ``TrigramIndexManager.exists()``/``.query()``) against
  the trigram index file, which ``trigram_index_manager.py``'s own
  docstring states "lives on shared NFS under the golden repo" in
  cluster mode -- a genuine synchronous DB/filesystem call, not a cheap
  in-memory check.

All three were called directly from ``search()`` (an ``async def``
awaited by both the REST route and the MCP handler) with no thread
offload at all.

Discriminating strategy: thread-identity capture (not timing), following
this project's own established pattern in
``test_regex_search_event_loop_offload_1601.py``. Every instrumentation
point here wraps a STANDARD-LIBRARY or third-party boundary the real code
calls into (``pathlib.Path.exists``, ``subprocess.run``,
``TrigramIndexManager.exists``) and delegates to the real implementation
-- never a stub/fake of ``RegexSearchService`` itself. ``search()`` runs
for real end-to-end against a real (empty) temp repository with the real
``rg``/``grep`` binary; nothing on the ``RegexSearchService`` class is
mocked away. Each capture is narrowly scoped to the SPECIFIC call under
test (matched by argument, e.g. the exact search_path instance or the
exact pcre2-probe command) and captured only on its FIRST matching
occurrence, so an unrelated later call to the same stdlib function
(elsewhere in the real end-to-end path) can never mask a genuine
regression by overwriting a correct-thread capture with a different
call's result.
"""

from __future__ import annotations

import shutil
import subprocess as subprocess_module
import threading
from pathlib import Path
from typing import Any, Dict

import pytest
from unittest.mock import patch

from code_indexer.global_repos.regex_search import RegexSearchService
from code_indexer.global_repos.trigram_index_manager import TrigramIndexManager

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None and shutil.which("grep") is None,
    reason="neither ripgrep nor grep available on this system",
)


class TestPreDispatchSyncCallsOffload:
    """Priority 3: search_path.exists(), _detect_pcre2_support(), and
    _prefilter_candidate_files() must all run off the event-loop thread
    when search() is awaited -- proven at the real stdlib/third-party
    boundary each one calls into, with no mocking of RegexSearchService
    itself."""

    @pytest.mark.asyncio
    async def test_search_path_exists_check_runs_off_event_loop_thread(self, tmp_path):
        service = RegexSearchService(tmp_path)
        target_path = service.repo_path  # exactly what search() checks
        caller_thread_id = threading.get_ident()
        captured: Dict[str, int] = {}

        real_exists = Path.exists

        def _wrapped_exists(self_path, *args, **kwargs):
            # Narrowly scoped: only the SPECIFIC search_path instance
            # search() checks, captured only on its first match, so an
            # unrelated Path.exists() call elsewhere in the real
            # end-to-end path cannot mask a wrong-thread regression by
            # overwriting a correct capture.
            if self_path == target_path and "thread_id" not in captured:
                captured["thread_id"] = threading.get_ident()
            return real_exists(self_path, *args, **kwargs)

        with patch.object(Path, "exists", autospec=True, side_effect=_wrapped_exists):
            # A real end-to-end call: no matches expected against an
            # empty temp repo, but search_path.exists() must fire first.
            await service.search(pattern="anything", max_results=10)

        assert "thread_id" in captured, "search_path.exists() was never invoked"
        assert captured["thread_id"] != caller_thread_id, (
            "search_path.exists() ran on the event-loop thread instead of "
            "being offloaded to a worker thread via anyio.to_thread.run_sync"
        )
        assert threading.get_ident() == caller_thread_id

    @pytest.mark.asyncio
    async def test_prefilter_candidate_files_runs_off_event_loop_thread(self, tmp_path):
        if shutil.which("rg") is None:
            pytest.skip("ripgrep required for the trigram pre-filter branch")

        service = RegexSearchService(tmp_path)
        caller_thread_id = threading.get_ident()
        captured: Dict[str, int] = {}

        real_index_exists = TrigramIndexManager.exists

        def _wrapped_index_exists(self_index, *args, **kwargs):
            if "thread_id" not in captured:
                captured["thread_id"] = threading.get_ident()
            return real_index_exists(self_index, *args, **kwargs)

        with patch.object(
            TrigramIndexManager,
            "exists",
            autospec=True,
            side_effect=_wrapped_index_exists,
        ):
            await service.search(pattern="anything", max_results=10)

        assert "thread_id" in captured, (
            "_prefilter_candidate_files (via TrigramIndexManager.exists) "
            "was never invoked"
        )
        assert captured["thread_id"] != caller_thread_id, (
            "_prefilter_candidate_files (real sqlite3 trigram-index query) "
            "ran on the event-loop thread instead of being offloaded to a "
            "worker thread via anyio.to_thread.run_sync"
        )
        assert threading.get_ident() == caller_thread_id

    @pytest.mark.asyncio
    async def test_detect_pcre2_support_runs_off_event_loop_thread(self, tmp_path):
        if shutil.which("rg") is None:
            pytest.skip("ripgrep required for the pcre2 probe")

        service = RegexSearchService(tmp_path)

        # Force a fresh probe for this test, regardless of what earlier
        # tests in the same process may have cached -- restore afterward
        # so this test does not leak process-wide state into others (the
        # same reset/restore contract TestDetectPcre2Support's own
        # autouse fixture uses).
        original_global = RegexSearchService._pcre2_supported_global
        RegexSearchService._pcre2_supported_global = None
        service._pcre2_supported = None

        caller_thread_id = threading.get_ident()
        captured: Dict[str, int] = {}

        real_run = subprocess_module.run
        expected_probe_cmd = ["rg", "--pcre2-version"]

        def _wrapped_run(*args: Any, **kwargs: Any) -> Any:
            # Narrowly scoped: only the exact PCRE2-probe command, so a
            # later unrelated subprocess.run() call (none expected on
            # this path today, but future-proofed) cannot mask a
            # wrong-thread regression on the probe itself.
            command = args[0] if args else kwargs.get("args")
            if command == expected_probe_cmd and "thread_id" not in captured:
                captured["thread_id"] = threading.get_ident()
            return real_run(*args, **kwargs)

        try:
            with patch(
                "code_indexer.global_repos.regex_search.subprocess.run",
                side_effect=_wrapped_run,
            ):
                try:
                    await service.search(pattern="anything", max_results=10, pcre2=True)
                except ValueError:
                    # PCRE2 genuinely unsupported by the installed rg build
                    # on this machine -- search() correctly rejects it.
                    # Irrelevant to this test: the probe still had to run
                    # (and be captured) to reach that determination.
                    pass
        finally:
            RegexSearchService._pcre2_supported_global = original_global

        assert "thread_id" in captured, "subprocess.run (pcre2 probe) was never invoked"
        assert captured["thread_id"] != caller_thread_id, (
            "_detect_pcre2_support (real subprocess.run() fork+exec on "
            "first call) ran on the event-loop thread instead of being "
            "offloaded to a worker thread via anyio.to_thread.run_sync"
        )
        assert threading.get_ident() == caller_thread_id
