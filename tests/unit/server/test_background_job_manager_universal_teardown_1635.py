"""
Bug #1635 (3rd review remediation): vector-agnostic universal teardown for
BackgroundJobManager instances.

The prior two remediation passes patched `create_fastapi_app` (the FastAPI
app factory) to track and tear down BackgroundJobManager instances built
via `create_app()`. That approach had two proven defects (a `functools.wraps`
gap that broke `inspect.getsource` introspection tests, and a `sys.modules.
get()` alias-detection ordering bug) AND missed a third leak vector
entirely: three production classes (`ActivatedRepoManager`,
`SemanticQueryManager`, `ActivatedRepoIndexManager`) construct a hidden
`BackgroundJobManager()` internally whenever no explicit
`background_job_manager=` is injected -- invisible to any mechanism keyed
off `create_fastapi_app` or `app.state`.

This module tests the vector-agnostic redesign: `conftest.py` monkeypatches
`BackgroundJobManager.__init__` at the CLASS level (for both the canonical
class object and the separate `src.`-prefixed alias class object -- proven
to be two genuinely distinct class objects for the same source file), so
every construction of a BackgroundJobManager during a test -- direct,
via create_app(), or via one of the three implicit-default production
classes -- is tracked and torn down at test end, regardless of caller
identity or import path.

IMPORTANT re: what is "the system under test" here. The function under
test, `_teardown_all_background_job_managers_impl`, is a conftest.py
fixture-generator whose entire documented job is to monkeypatch
`BackgroundJobManager.__init__` for the duration of a test and reap every
instance created through it. Calling that real function directly (via a
manually created `pytest.MonkeyPatch()` and `next()`) and observing real
OS thread creation/teardown as the assertion oracle is exercising the SUT,
not mocking it -- there is no test double standing in for
BackgroundJobManager or for the generator; both are the real,
unmodified production/test-infrastructure code.

Because the REAL autouse fixture of the same name is ALSO active for every
test in this suite (this file lives under tests/unit/server/), any
BackgroundJobManager constructed here is necessarily tracked by BOTH the
manually-driven generator under test AND the real active fixture -- this
is intentional and harmless: BackgroundJobManager.shutdown() is proven
idempotent (see TestDoubleShutdownIsIdempotent below), so being torn down
twice (once by the manual generator in the test body, once more by the
real fixture's own teardown) is safe by design.
"""

from __future__ import annotations

import importlib
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, Set

import pytest

from tests.unit.server.conftest import (
    _resolve_background_job_manager_classes,
    _teardown_all_background_job_managers_impl,
)


def _bgm_thread_names() -> Set[str]:
    return {t.name for t in threading.enumerate() if t.name.startswith("bgm-")}


@contextmanager
def _driven_universal_teardown() -> Iterator[None]:
    """Drive `_teardown_all_background_job_managers_impl` exactly the way
    pytest's real autouse fixture would (enter, run test body, tear down),
    but manually. `mp.undo()` sits in the outermost `finally` so the
    MonkeyPatch is ALWAYS reverted -- whether generator construction, the
    first `next(gen)`, the caller's `yield` body, or the final `next(gen)`
    raises."""
    mp = pytest.MonkeyPatch()
    try:
        gen = _teardown_all_background_job_managers_impl(mp)
        next(gen)
        try:
            yield
        finally:
            try:
                next(gen)
            except StopIteration:
                pass
    finally:
        mp.undo()


def _assert_construction_creates_and_reaps_bgm_threads(
    construct: Callable[[], object],
) -> None:
    """Shared assertion body for every "construct X, expect its
    BackgroundJobManager threads to appear then be reaped" test below."""
    before = _bgm_thread_names()
    with _driven_universal_teardown():
        construct()
        during = _bgm_thread_names()
        assert during - before, (
            "expected new bgm-* worker threads while manager is alive"
        )
    after = _bgm_thread_names()
    assert after == before, f"expected no leaked bgm-* threads, found {after - before}"


class TestResolveBackgroundJobManagerClasses:
    def test_returns_canonical_and_alias_as_distinct_class_objects(self) -> None:
        classes = _resolve_background_job_manager_classes()

        assert len(classes) == 2
        canonical_cls, alias_cls = classes
        assert canonical_cls is not alias_cls
        assert canonical_cls.__name__ == "BackgroundJobManager"
        assert alias_cls.__name__ == "BackgroundJobManager"

    def test_alias_class_matches_forced_alias_import(self) -> None:
        classes = _resolve_background_job_manager_classes()
        alias_module = importlib.import_module(
            "src.code_indexer.server.repositories.background_jobs"
        )
        assert classes[1] is alias_module.BackgroundJobManager


class TestDirectConstructionViaCanonicalClassIsTornDown:
    def test_canonical_direct_construction_threads_cleaned_up(self, tmp_path) -> None:
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        _assert_construction_creates_and_reaps_bgm_threads(
            lambda: BackgroundJobManager(storage_path=str(tmp_path / "jobs.json"))
        )


class TestDirectConstructionViaAliasClassIsTornDown:
    def test_alias_direct_construction_threads_cleaned_up(self, tmp_path) -> None:
        alias_module = importlib.import_module(
            "src.code_indexer.server.repositories.background_jobs"
        )
        alias_bgm_class = alias_module.BackgroundJobManager

        _assert_construction_creates_and_reaps_bgm_threads(
            lambda: alias_bgm_class(storage_path=str(tmp_path / "jobs.json"))
        )


class TestImplicitDefaultBackgroundJobManagerIsTornDown:
    """Fix 3: three production classes silently construct their own
    BackgroundJobManager() when none is injected. Verify the universal
    mechanism catches all three, with no explicit background_job_manager=
    passed to any of them."""

    def test_activated_repo_manager_implicit_default_bgm_is_torn_down(
        self, tmp_path
    ) -> None:
        from code_indexer.server.repositories.activated_repo_manager import (
            ActivatedRepoManager,
        )

        _assert_construction_creates_and_reaps_bgm_threads(
            lambda: ActivatedRepoManager(data_dir=str(tmp_path))
        )

    def test_semantic_query_manager_implicit_default_bgm_is_torn_down(
        self, tmp_path
    ) -> None:
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )

        _assert_construction_creates_and_reaps_bgm_threads(
            lambda: SemanticQueryManager(data_dir=str(tmp_path))
        )

    def test_activated_repo_index_manager_implicit_default_bgm_is_torn_down(
        self, tmp_path
    ) -> None:
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )

        _assert_construction_creates_and_reaps_bgm_threads(
            lambda: ActivatedRepoIndexManager(data_dir=str(tmp_path))
        )


class TestSurvivesEarlyTestFailureBeforeExplicitShutdown:
    def test_teardown_still_reaps_threads_when_test_body_never_calls_shutdown(
        self, tmp_path
    ) -> None:
        """Fix 4 coverage check: a test that raises BEFORE reaching its own
        explicit .shutdown() call (e.g. an earlier assertion failure) must
        still have its BackgroundJobManager's threads reaped at teardown,
        independent of whether the test body's own cleanup code ever ran."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        before = _bgm_thread_names()
        with pytest.raises(AssertionError, match="simulated early test failure"):
            with _driven_universal_teardown():
                # Deliberately never call manager.shutdown() -- proving the
                # universal mechanism does not depend on it.
                BackgroundJobManager(storage_path=str(tmp_path / "jobs.json"))
                raise AssertionError("simulated early test failure before .shutdown()")

        after = _bgm_thread_names()
        assert after == before, (
            f"expected no leaked bgm-* threads, found {after - before}"
        )


class TestDoubleShutdownIsIdempotent:
    def test_calling_shutdown_twice_does_not_raise_and_fully_stops_threads(
        self, tmp_path
    ) -> None:
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        manager = BackgroundJobManager(storage_path=str(tmp_path / "jobs.json"))
        manager_thread_names = {
            name for name in _bgm_thread_names() if str(id(manager)) in name
        }
        assert manager_thread_names, "expected this manager's own bgm-* threads"

        manager.shutdown()
        manager.shutdown()  # must not raise, must remain a no-op

        remaining = _bgm_thread_names() & manager_thread_names
        assert not remaining, (
            f"expected this manager's threads to be gone, found {remaining}"
        )
