"""Issue #1609: RegexSearchService.__init__() performs synchronous
Path.resolve() and shutil.which() calls directly in the constructor. The
MCP handler's ``_execute_regex_search_impl`` (an ``async def``, reached
from ``handle_regex_search``) constructs ``RegexSearchService`` directly
on its own thread -- the asyncio event-loop thread -- which is this
project's own documented Production Scale invariant violation: "NEVER
call a synchronous filesystem/network function directly inside `async
def`... Offload with `anyio.to_thread.run_sync(...)`." (CLAUDE.md).

``Path.resolve()`` is a real filesystem call that can block forever on a
`hard` NFSv3 mount; ``shutil.which()`` performs real PATH-directory
stat/exec-bit probes, also a synchronous filesystem operation.

Discriminating test strategy: thread-identity capture, mirroring the
established pattern in
tests/unit/global_repos/test_regex_search_event_loop_offload_1601.py --
un-offloaded code ALWAYS records the same OS thread identity as the
caller; a genuine ``anyio.to_thread.run_sync`` offload ALWAYS records a
different one.

The spies are SCOPED TO THE CONSTRUCTOR: a thin spy around the real
``RegexSearchService.__init__`` records the thread it runs on and sets a
thread-local flag for its duration; the ``Path.resolve``/``shutil.which``
spies record a thread identity only while that flag is set on the calling
thread. Unscoped, process-wide spies are order-dependent: when this test
runs alone, the handler's ``_get_wiki_enabled_repos`` is the first access
to ``code_indexer.server.app``'s lazy attributes, which builds the whole
server app (``create_app`` -> primary-instance lock -> ``Path.resolve``)
on the event-loop thread -- an unrelated, test-only warm-up that an earlier
test normally performs. Scoping makes the guard measure exactly the
constructor's own calls regardless of what else the process does.

This is a UNIT test of ``_execute_regex_search_impl``. The only mocked
collaborator is ``get_config_service`` (a legitimate external
configuration dependency, unrelated to the mechanism under test).
``RegexSearchService`` itself and its ``search()`` method are never
mocked or replaced -- the constructor and the real ripgrep/grep engine
run for real against a real temp-directory repo; every spy delegates to
the genuine implementation. The lazy trigram-index background-build thread
(a genuinely separate, unrelated code path) is disabled via
CIDX_TRIGRAM_LAZY_BUILD=0.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.search import _execute_regex_search_impl
import code_indexer.global_repos.regex_search as regex_search_module

_TEST_SEARCH_TIMEOUT_SECONDS = 30
_TEST_SUBPROCESS_MAX_WORKERS = 2
_NON_MATCHING_PATTERN = "no_such_pattern_will_ever_match_xyz123"


def _make_user() -> User:
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


def _make_mock_config() -> Mock:
    mock_config = Mock()
    mock_config.search_limits_config.timeout_seconds = _TEST_SEARCH_TIMEOUT_SECONDS
    mock_config.background_jobs_config.subprocess_max_workers = (
        _TEST_SUBPROCESS_MAX_WORKERS
    )
    return mock_config


@dataclass
class _ConstructorSpies:
    """Thread identities recorded while RegexSearchService.__init__ runs,
    plus the delegating spy callables to patch in."""

    init_threads: List[int] = field(default_factory=list)
    resolve_threads: List[int] = field(default_factory=list)
    which_threads: List[int] = field(default_factory=list)
    spy_init: Callable[..., None] = field(init=False)
    spy_resolve: Callable[..., Path] = field(init=False)
    spy_which: Callable[..., Any] = field(init=False)


def _install_constructor_scoped_spies() -> _ConstructorSpies:
    """Wrap the real RegexSearchService.__init__, Path.resolve and
    shutil.which. resolve/which calls are recorded only while the real
    constructor is executing on the calling thread; every spy delegates to
    the genuine implementation."""
    spies = _ConstructorSpies()
    in_constructor = threading.local()
    real_init = regex_search_module.RegexSearchService.__init__
    real_resolve = Path.resolve
    real_which = shutil.which

    def _spy_init(self_service: Any, *args: Any, **kwargs: Any) -> None:
        spies.init_threads.append(threading.get_ident())
        in_constructor.active = True
        try:
            real_init(self_service, *args, **kwargs)
        finally:
            in_constructor.active = False

    def _spy_resolve(self_path: Path, *args: Any, **kwargs: Any) -> Path:
        if getattr(in_constructor, "active", False):
            spies.resolve_threads.append(threading.get_ident())
        return real_resolve(self_path, *args, **kwargs)

    def _spy_which(cmd: str, *args: Any, **kwargs: Any) -> Any:
        if getattr(in_constructor, "active", False):
            spies.which_threads.append(threading.get_ident())
        return real_which(cmd, *args, **kwargs)

    spies.spy_init = _spy_init
    spies.spy_resolve = _spy_resolve
    spies.spy_which = _spy_which
    return spies


class TestRegexSearchServiceConstructorOffloadMcp:
    """The synchronous Path.resolve()/shutil.which() calls performed by
    RegexSearchService.__init__() must run off the event-loop thread when
    the service is constructed from _execute_regex_search_impl."""

    @pytest.mark.asyncio
    async def test_constructor_resolve_and_which_run_off_event_loop_thread(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("CIDX_TRIGRAM_LAZY_BUILD", "0")
        (tmp_path / "sample.py").write_text("def real_function():\n    pass\n")

        main_thread_id = threading.get_ident()
        spies = _install_constructor_scoped_spies()

        with (
            patch.object(
                regex_search_module.RegexSearchService, "__init__", spies.spy_init
            ),
            patch.object(regex_search_module.Path, "resolve", spies.spy_resolve),
            patch.object(regex_search_module.shutil, "which", spies.spy_which),
            patch(
                "code_indexer.server.mcp.handlers.search.regex_search.get_config_service"
            ) as mock_get_config_service,
        ):
            mock_get_config_service.return_value.get_config.return_value = (
                _make_mock_config()
            )
            await _execute_regex_search_impl(
                {"pattern": _NON_MATCHING_PATTERN},
                tmp_path,
                "myrepo-global",
                _make_user(),
            )

        self._assert_all_offloaded(spies, main_thread_id)

    @staticmethod
    def _assert_all_offloaded(spies: _ConstructorSpies, main_thread_id: int) -> None:
        assert len(spies.init_threads) == 1, (
            f"RegexSearchService constructed {len(spies.init_threads)} times, "
            "expected exactly once"
        )
        assert spies.resolve_threads, "constructor never called Path.resolve()"
        assert spies.which_threads, "constructor never called shutil.which()"
        assert spies.init_threads[0] != main_thread_id, (
            "RegexSearchService was constructed on the event-loop (calling) "
            "thread instead of being offloaded via anyio.to_thread.run_sync"
        )
        assert all(tid != main_thread_id for tid in spies.resolve_threads), (
            "RegexSearchService constructor's Path.resolve() ran on the "
            "event-loop (calling) thread instead of being offloaded via "
            "anyio.to_thread.run_sync"
        )
        assert all(tid != main_thread_id for tid in spies.which_threads), (
            "RegexSearchService constructor's shutil.which() ran on the "
            "event-loop (calling) thread instead of being offloaded via "
            "anyio.to_thread.run_sync"
        )
