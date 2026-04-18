"""
Unit tests for Story #724 verification pass — shared semaphore (AC5).

Tests: TestSemaphoreSingleton, TestSemaphoreConcurrencyCap
"""

import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional
from unittest import TestCase
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.dependency_map_analyzer import (
    DependencyMapAnalyzer,
    _VERIFICATION_SEMAPHORE_STATE,
    _get_verification_semaphore,
)

# Threading coordination timeouts (seconds).
# Large enough to avoid false failures on slow CI; small enough to fail fast.
_SYNC_TIMEOUT: float = 5.0  # max wait for a condition predicate to become true
_JOIN_TIMEOUT: float = 10.0  # max wait for threads to finish after release

_FAKE_STDOUT_EMPTY = '{"is_error": false, "result": "{}"}'

# Seconds to advance doc_path mtime in fake_run so the mtime-change postcondition
# is satisfied deterministically on all filesystems regardless of timestamp resolution.
_DOC_MTIME_ADVANCE_SECONDS = 2.0


def _semaphore_with_bad_arg(value: object) -> None:
    """Call _get_verification_semaphore with a deliberately invalid argument type.

    Accepts `object` at the call site so no type: ignore is needed by test methods.
    The single type: ignore[arg-type] below is localized here and justified: this
    helper exists solely to test that _get_verification_semaphore rejects invalid
    types, so passing a non-int value intentionally is the whole point.
    """
    _get_verification_semaphore(value)  # type: ignore[arg-type]


@dataclass
class _SharedState:
    """Typed shared state for concurrency coordination."""

    active: int = 0
    peak: int = 0
    released: bool = False
    condition: threading.Condition = field(default_factory=threading.Condition)


def _make_doc_path_setup(
    tmp_path: Path,
) -> tuple:
    """Return (doc_path_factory, doc_path_local) for use in concurrent tests.

    Each call to doc_path_factory() creates a unique numbered document file
    under tmp_path.  doc_path_local is a threading.local whose .path attribute
    each caller thread sets to its own document file before invoking
    invoke_verification_pass, so that fake_run can write to the correct
    per-thread file without racing against another thread's reseed.
    """
    counter: List[int] = [0]
    counter_lock = threading.Lock()
    doc_path_local: threading.local = threading.local()

    def doc_path_factory() -> Path:
        with counter_lock:
            counter[0] += 1
            idx = counter[0]
        p = tmp_path / f"test_doc_{idx}.md"
        p.write_text("# Test\n\nSome content for verification.\n")
        return p

    return doc_path_factory, doc_path_local


def _make_analyzer(tmp_dir: Path) -> DependencyMapAnalyzer:
    """Return a minimally configured analyzer using a portable temp directory."""
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_dir / "golden-repos",
        cidx_meta_path=tmp_dir / "cidx-meta",
        pass_timeout=60,
        analysis_model="opus",
    )


def _make_config(max_concurrent: int = 2) -> MagicMock:
    cfg = MagicMock()
    cfg.fact_check_timeout_seconds = 10
    cfg.dependency_map_delta_max_turns = 5
    cfg.max_concurrent_claude_cli = max_concurrent
    return cfg


def _make_blocking_fake_run(
    cap: int, shared: _SharedState, doc_path_local: threading.local
) -> Callable:
    """Return a fake subprocess.run that blocks until shared.released is True.

    All coordination uses shared.condition:
    - Workers increment shared.active on entry and notify when active reaches cap.
    - Workers block on condition.wait_for(lambda: shared.released).
      The return value is checked; RuntimeError on timeout surfaces failures
      rather than silently returning a success result.
    - Release: set shared.released = True then condition.notify_all().

    On return, writes different content to the per-thread doc_path stored in
    doc_path_local.path so the content-change postcondition is satisfied
    deterministically.  Each caller thread stores its own Path in
    doc_path_local.path before invoking invoke_verification_pass, eliminating
    the reseed race where invoke_verification_pass's per-attempt reseed writes
    original_content back to a shared path while another thread's fake_run is
    still using that same path.
    Returns FILE_EDIT_COMPLETE in stdout to satisfy the sentinel postcondition.
    """

    def fake_run(*args, **kwargs):
        with shared.condition:
            shared.active += 1
            if shared.active > shared.peak:
                shared.peak = shared.active
            if shared.active == cap:
                shared.condition.notify_all()
            try:
                released = shared.condition.wait_for(
                    lambda: shared.released, timeout=_SYNC_TIMEOUT
                )
            finally:
                # Always restore active count so shared state stays consistent
                # on both the normal path and the timeout-error path.
                shared.active -= 1
            if not released:
                raise RuntimeError(
                    f"fake_run timed out waiting for release after {_SYNC_TIMEOUT}s"
                )
        # threading.local attributes are set by each caller thread before it
        # starts invoke_verification_pass, so .path is guaranteed to exist here.
        # getattr avoids a type: ignore on the dynamic attribute access.
        thread_doc_path: Path = getattr(doc_path_local, "path")
        thread_doc_path.write_text("# Test\n\nVerified.\n")
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="FILE_EDIT_COMPLETE", stderr=""
        )

    return fake_run


def _run_concurrent_callers(
    num_callers: int,
    analyzer: DependencyMapAnalyzer,
    config: MagicMock,
    shared: _SharedState,
    fake_run: Callable,
    cap: int,
    completed_count: List[int],
    count_lock: threading.Lock,
    errors: List[Exception],
    errors_lock: threading.Lock,
    doc_path_factory: Callable[[], Path],
    doc_path_local: threading.local,
    at_cap_callback: Optional[Callable[[_SharedState], None]] = None,
) -> None:
    """Start threads, wait deterministically for cap to be active, then release.

    Each caller receives its own doc_path from doc_path_factory() and stores it
    in doc_path_local.path before calling invoke_verification_pass.  This
    eliminates the reseed race where invoke_verification_pass's per-attempt
    reseed writes original_content back to a shared path while fake_run in
    another thread is trying to write different content to satisfy the
    content-change postcondition.

    `at_cap_callback` is invoked (when provided) while exactly `cap` threads are
    blocked inside fake_run — useful for asserting peak/active counts mid-run.
    Omit it when the test only cares about eventual completion.

    Asserts every thread terminates within _JOIN_TIMEOUT after release.
    All worker exceptions are captured into `errors`.
    """

    def caller() -> None:
        # Each thread gets its own document file — no shared-file race with
        # invoke_verification_pass's per-attempt reseed.
        my_doc_path = doc_path_factory()
        doc_path_local.path = my_doc_path
        try:
            analyzer.invoke_verification_pass(
                document_path=my_doc_path,
                repo_list=[],
                config=config,
            )
            with count_lock:
                completed_count[0] += 1
        except Exception as exc:
            with errors_lock:
                errors.append(exc)

    with patch("subprocess.run", side_effect=fake_run):
        threads = [
            threading.Thread(target=caller, daemon=True) for _ in range(num_callers)
        ]
        for t in threads:
            t.start()

        with shared.condition:
            reached = shared.condition.wait_for(
                lambda: shared.active == cap, timeout=_SYNC_TIMEOUT
            )
        assert reached, f"Timed out: {cap} threads did not reach subprocess.run"

        if at_cap_callback is not None:
            at_cap_callback(shared)

        with shared.condition:
            shared.released = True
            shared.condition.notify_all()

        for t in threads:
            t.join(timeout=_JOIN_TIMEOUT)
            assert not t.is_alive(), (
                f"Thread {t.name} did not terminate within {_JOIN_TIMEOUT}s"
            )


class TestSemaphoreSingleton(TestCase):
    """Tests for _get_verification_semaphore singleton contract."""

    def setUp(self) -> None:
        _VERIFICATION_SEMAPHORE_STATE.clear()

    def test_same_value_returns_same_object(self) -> None:
        s1 = _get_verification_semaphore(2)
        s2 = _get_verification_semaphore(2)
        self.assertIs(s1, s2)

    def test_different_value_raises_value_error(self) -> None:
        _get_verification_semaphore(2)
        with self.assertRaises(ValueError):
            _get_verification_semaphore(3)

    def test_zero_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            _get_verification_semaphore(0)

    def test_negative_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            _get_verification_semaphore(-1)

    def test_non_integer_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            _semaphore_with_bad_arg(2.0)

    def test_bool_raises_value_error(self) -> None:
        # bool is a subclass of int but must be rejected by the isinstance(..., bool) guard
        with self.assertRaises(ValueError):
            _semaphore_with_bad_arg(True)


class TestSemaphoreConcurrencyCap(TestCase):
    """Tests that cap=2 limits concurrent callers to exactly 2 at a time."""

    def setUp(self) -> None:
        _VERIFICATION_SEMAPHORE_STATE.clear()
        self._tmp = tempfile.mkdtemp()
        self._tmp_path = Path(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_semaphore_caps_concurrent_callers_at_exactly_cap(self) -> None:
        """With cap=2, exactly 2 threads reach subprocess.run while the 3rd waits."""
        cap = 2
        num_callers = 3
        config = _make_config(max_concurrent=cap)
        analyzer = _make_analyzer(self._tmp_path)
        doc_path_factory, doc_path_local = _make_doc_path_setup(self._tmp_path)
        shared = _SharedState()
        fake_run = _make_blocking_fake_run(cap, shared, doc_path_local)
        completed_count: List[int] = [0]
        count_lock = threading.Lock()
        errors: List[Exception] = []
        errors_lock = threading.Lock()

        active_snapshot: List[int] = [0]
        peak_snapshot: List[int] = [0]

        def assert_at_cap(s: _SharedState) -> None:
            with s.condition:
                active_snapshot[0] = s.active
                peak_snapshot[0] = s.peak

        _run_concurrent_callers(
            num_callers=num_callers,
            analyzer=analyzer,
            config=config,
            shared=shared,
            fake_run=fake_run,
            cap=cap,
            completed_count=completed_count,
            count_lock=count_lock,
            errors=errors,
            errors_lock=errors_lock,
            doc_path_factory=doc_path_factory,
            doc_path_local=doc_path_local,
            at_cap_callback=assert_at_cap,
        )

        self.assertEqual(
            active_snapshot[0],
            cap,
            f"Expected exactly {cap} active at cap, got {active_snapshot[0]}",
        )
        self.assertEqual(
            peak_snapshot[0], cap, f"Expected peak of {cap}, got {peak_snapshot[0]}"
        )
        with errors_lock:
            self.assertEqual(errors, [], f"Thread errors: {errors}")

    def test_after_release_all_threads_complete(self) -> None:
        """All callers eventually complete after the release signal is set."""
        cap = 2
        num_callers = 3
        config = _make_config(max_concurrent=cap)
        analyzer = _make_analyzer(self._tmp_path)
        doc_path_factory, doc_path_local = _make_doc_path_setup(self._tmp_path)
        shared = _SharedState()
        fake_run = _make_blocking_fake_run(cap, shared, doc_path_local)
        completed_count: List[int] = [0]
        count_lock = threading.Lock()
        errors: List[Exception] = []
        errors_lock = threading.Lock()

        _run_concurrent_callers(
            num_callers=num_callers,
            analyzer=analyzer,
            config=config,
            shared=shared,
            fake_run=fake_run,
            cap=cap,
            completed_count=completed_count,
            count_lock=count_lock,
            errors=errors,
            errors_lock=errors_lock,
            doc_path_factory=doc_path_factory,
            doc_path_local=doc_path_local,
        )

        with errors_lock:
            self.assertEqual(errors, [], f"Thread errors: {errors}")
        self.assertEqual(completed_count[0], num_callers)
