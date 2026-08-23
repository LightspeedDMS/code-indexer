"""Issue #1609 (REST route side): RegexSearchService.__init__() performs
synchronous Path.resolve() and shutil.which() calls directly in the
constructor. The REST route's ``_execute_single_search`` (an ``async
def``, reached from ``POST /api/regex/search``) constructs
``RegexSearchService`` directly on its own thread -- the asyncio
event-loop thread -- which is this project's own documented Production
Scale invariant violation: "NEVER call a synchronous filesystem/network
function directly inside `async def`... Offload with
`anyio.to_thread.run_sync(...)`." (CLAUDE.md).

``Path.resolve()`` is a real filesystem call that can block forever on a
`hard` NFSv3 mount; ``shutil.which()`` performs real PATH-directory
stat/exec-bit probes, also a synchronous filesystem operation.

Discriminating test strategy: thread-identity capture, mirroring the
established pattern in
tests/unit/global_repos/test_regex_search_event_loop_offload_1601.py and
the sibling MCP-side test
tests/unit/server/mcp/test_handlers_regex_search_constructor_offload_1609.py
-- un-offloaded code ALWAYS records the same OS thread identity as the
caller; a genuine ``anyio.to_thread.run_sync`` offload ALWAYS records a
different one.

This is a UNIT test of ``_execute_single_search``. Neither
``RegexSearchService`` nor its ``search()`` method is mocked or
replaced -- the constructor and the real ripgrep/grep engine run for
real against a real temp-directory repo. The search pattern is
deliberately chosen to match nothing, so the ONLY Path.resolve() call in
the whole flow is RegexSearchService.__init__'s own
``Path(repo_path).resolve()`` and the ONLY shutil.which() calls are
_detect_search_engine's own probes -- both happen exclusively during
construction. The lazy trigram-index background-build thread (a
genuinely separate, unrelated code path) is disabled via
CIDX_TRIGRAM_LAZY_BUILD=0 so it cannot fire an unrelated background
Path.resolve() call during the test.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any, Callable, List, Tuple

import pytest

from code_indexer.server.routes.regex_routes import (
    RegexSearchRequest,
    _execute_single_search,
)
import code_indexer.global_repos.regex_search as regex_search_module

_NON_MATCHING_PATTERN = "no_such_pattern_will_ever_match_xyz123"


def _install_thread_capturing_spies() -> Tuple[
    List[int], List[int], Callable[..., Path], Callable[..., object]
]:
    """Wrap the real Path.resolve/shutil.which so each call records the OS
    thread identity it executed on, then delegates to the real
    implementation. Returns (resolve_threads, which_threads, spy_resolve,
    spy_which) for the caller to patch in.

    ``resolve_threads``/``which_threads`` are plain lists, not
    thread-locked: this is intentionally safe because CPython's GIL makes
    a single ``list.append()`` atomic at the bytecode level, and this
    test's own resolve()/which() calls always happen sequentially with
    respect to each other (either directly on the caller's thread when
    un-offloaded, or one-at-a-time on anyio's worker thread when
    offloaded) -- there is no concurrent-append scenario to protect
    against here.
    """
    resolve_threads: List[int] = []
    which_threads: List[int] = []
    real_resolve = Path.resolve
    real_which = shutil.which

    # `*args`/`**kwargs` typed as `Any` (not a precise signature) because
    # both Path.resolve and shutil.which (`mode`, `path` keyword args)
    # are overloaded/versioned stdlib signatures; this spy only needs to
    # transparently forward whatever it receives to the real
    # implementation, not type-check it.
    def _spy_resolve(self_path: Path, *args: Any, **kwargs: Any) -> Path:
        resolve_threads.append(threading.get_ident())
        return real_resolve(self_path, *args, **kwargs)

    def _spy_which(cmd: str, *args: Any, **kwargs: Any) -> Any:
        which_threads.append(threading.get_ident())
        return real_which(cmd, *args, **kwargs)

    return resolve_threads, which_threads, _spy_resolve, _spy_which


class TestRegexSearchServiceConstructorOffloadRest:
    """The synchronous Path.resolve()/shutil.which() calls performed by
    RegexSearchService.__init__() must run off the event-loop thread when
    the service is constructed from _execute_single_search (REST route)."""

    @pytest.mark.asyncio
    async def test_constructor_resolve_and_which_run_off_event_loop_thread(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("CIDX_TRIGRAM_LAZY_BUILD", "0")
        (tmp_path / "sample.py").write_text("def real_function():\n    pass\n")

        main_thread_id = threading.get_ident()
        resolve_threads, which_threads, spy_resolve, spy_which = (
            _install_thread_capturing_spies()
        )

        body = RegexSearchRequest(
            pattern=_NON_MATCHING_PATTERN, repository_alias="myrepo-global"
        )

        with (
            pytest.MonkeyPatch.context() as mp,
        ):
            mp.setattr(regex_search_module.Path, "resolve", spy_resolve)
            mp.setattr(regex_search_module.shutil, "which", spy_which)
            # Pattern deliberately matches nothing, so no per-match
            # _to_repo_relative() resolve() calls occur -- the only
            # resolve()/which() calls in this whole flow come from
            # RegexSearchService.__init__.
            await _execute_single_search(body, str(tmp_path))

        self._assert_all_offloaded(resolve_threads, which_threads, main_thread_id)

    @staticmethod
    def _assert_all_offloaded(
        resolve_threads: List[int], which_threads: List[int], main_thread_id: int
    ) -> None:
        assert resolve_threads, "Path.resolve() was never called"
        assert which_threads, "shutil.which() was never called"
        assert all(tid != main_thread_id for tid in resolve_threads), (
            "RegexSearchService constructor's Path.resolve() ran on the "
            "event-loop (calling) thread instead of being offloaded via "
            "anyio.to_thread.run_sync"
        )
        assert all(tid != main_thread_id for tid in which_threads), (
            "RegexSearchService constructor's shutil.which() ran on the "
            "event-loop (calling) thread instead of being offloaded via "
            "anyio.to_thread.run_sync"
        )
