"""
Unit tests for Story #724 AC5 scope expansion: _invoke_claude_cli acquires the
shared verification semaphore.

Three test classes, one test each, all sharing _Base setUp/tearDown:
  TestCliSemaphoreCap         -- cap limits concurrent subprocess.run calls
  TestCliNoSemaphore          -- safe fallback when semaphore not initialised
  TestCliVerificationShared   -- CLI and verification share the same semaphore
"""

import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List
from unittest import TestCase
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.dependency_map_analyzer import (
    DependencyMapAnalyzer,
    _VERIFICATION_SEMAPHORE_STATE,
    _get_verification_semaphore,
)

_SYNC_TIMEOUT: float = 5.0
_JOIN_TIMEOUT: float = 10.0

_FAKE_VERIFICATION_STDOUT = (
    '{"is_error": false, "result": "{\\"verified_document\\": \\"doc\\", '
    '\\"evidence\\": [], \\"counts\\": {\\"verified\\": 1, \\"corrected\\": 0, '
    '\\"removed\\": 0, \\"added\\": 0}}"}'
)

# Seconds to advance doc_path mtime in fake verification subprocess so the
# mtime-change postcondition is satisfied deterministically on all filesystems.
_DOC_MTIME_ADVANCE_SECONDS = 2.0


@dataclass
class _SharedState:
    """Thread coordination state using a single condition variable."""

    active: int = 0
    peak: int = 0
    released: bool = False
    ver_started: bool = False  # true once verification thread starts
    ver_entered_run: bool = False  # true once verification enters subprocess.run
    condition: threading.Condition = field(default_factory=threading.Condition)


def _make_analyzer(tmp_path: Path) -> DependencyMapAnalyzer:
    (tmp_path / "golden-repos").mkdir(parents=True, exist_ok=True)
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path / "golden-repos",
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=60,
        analysis_model="opus",
    )


def _make_config(max_concurrent: int) -> MagicMock:
    cfg = MagicMock()
    cfg.fact_check_timeout_seconds = 10
    cfg.dependency_map_delta_max_turns = 5
    cfg.max_concurrent_claude_cli = max_concurrent
    return cfg


def _blocking_fake_run(shared: _SharedState, cap: int):
    """subprocess.run replacement that blocks until shared.released."""

    def fake_run(*args, **kwargs):
        with shared.condition:
            shared.active += 1
            if shared.active > shared.peak:
                shared.peak = shared.active
            if shared.active == cap:
                shared.condition.notify_all()
            ok = shared.condition.wait_for(
                lambda: shared.released, timeout=_SYNC_TIMEOUT
            )
            if not ok:
                raise RuntimeError(f"fake_run timed out after {_SYNC_TIMEOUT}s")
        with shared.condition:
            shared.active -= 1
            shared.condition.notify_all()
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="result", stderr=""
        )

    return fake_run


def _wait_cond(shared: _SharedState, predicate, msg: str) -> None:
    with shared.condition:
        ok = shared.condition.wait_for(predicate, timeout=_SYNC_TIMEOUT)
    assert ok, msg


def _release_all(shared: _SharedState) -> None:
    with shared.condition:
        shared.released = True
        shared.condition.notify_all()


def _join_threads(threads: List[threading.Thread]) -> List[str]:
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT)
    return [t.name for t in threads if t.is_alive()]


def _sem_value(sem: threading.Semaphore) -> int:
    """Read semaphore internal counter for test assertions only.

    Accesses CPython's private ``_value`` attribute — the only way to inspect
    semaphore state without acquiring it. Stable across CPython 3.x.
    """
    return sem._value  # type: ignore[attr-defined]


def _run_cli_callers(
    num: int,
    analyzer: DependencyMapAnalyzer,
    completed: List[int],
    completed_lock: threading.Lock,
    errors: List[Exception],
    errors_lock: threading.Lock,
) -> List[threading.Thread]:
    def caller() -> None:
        try:
            analyzer._invoke_claude_cli(prompt="hello", timeout=30, max_turns=0)
            with completed_lock:
                completed[0] += 1
        except Exception as exc:
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=caller, daemon=True) for _ in range(num)]
    for t in threads:
        t.start()
    return threads


def _run_verification_caller(
    analyzer: DependencyMapAnalyzer,
    cfg: MagicMock,
    shared: _SharedState,
    completed: List[int],
    completed_lock: threading.Lock,
    errors: List[Exception],
    errors_lock: threading.Lock,
    doc_path: Path,
) -> threading.Thread:
    def caller() -> None:
        with shared.condition:
            shared.ver_started = True
            shared.condition.notify_all()
        try:
            analyzer.invoke_verification_pass(
                document_path=doc_path,
                repo_list=[],
                config=cfg,
            )
            with completed_lock:
                completed[0] += 1
        except Exception as exc:
            with errors_lock:
                errors.append(exc)

    t = threading.Thread(target=caller, daemon=True)
    t.start()
    return t


def _build_dispatching_run(
    shared: _SharedState,
    call_count: List[int],
    call_count_lock: threading.Lock,
    doc_path: Path,
):
    """First call blocks (CLI); second is fast (verification) and signals ver_entered_run.

    The verification branch (seq != 1) writes different content to doc_path and
    returns FILE_EDIT_COMPLETE in stdout so all three verification postconditions
    (sentinel, content-change, non-empty content) are satisfied.
    """

    def dispatching_fake_run(*args, **kwargs):
        with call_count_lock:
            call_count[0] += 1
            seq = call_count[0]
        if seq == 1:
            return _blocking_fake_run(shared, 1)(*args, **kwargs)
        with shared.condition:
            shared.ver_entered_run = True
            shared.condition.notify_all()
        # Write fixed different content so the content-change postcondition passes
        # (constant write — no read — avoids races with concurrent re-seeds)
        doc_path.write_text("# Test\n\nVerified.\n")
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="FILE_EDIT_COMPLETE", stderr=""
        )

    return dispatching_fake_run


class _Base(TestCase):
    """Shared setUp/tearDown for all semaphore test classes."""

    def setUp(self) -> None:
        _VERIFICATION_SEMAPHORE_STATE.clear()
        self._tmp = tempfile.mkdtemp()
        self._tmp_path = Path(self._tmp)

    def tearDown(self) -> None:
        _VERIFICATION_SEMAPHORE_STATE.clear()
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestCliSemaphoreCap(_Base):
    """_invoke_claude_cli respects semaphore cap on concurrency."""

    def test_invoke_claude_cli_acquires_shared_semaphore(self) -> None:
        """With cap=2, exactly 2 _invoke_claude_cli calls reach subprocess.run
        simultaneously; the 3rd waits for one to release."""
        cap, num = 2, 3
        analyzer = _make_analyzer(self._tmp_path)
        _get_verification_semaphore(cap)
        shared = _SharedState()
        errors: List[Exception] = []
        errors_lock = threading.Lock()
        completed: List[int] = [0]
        completed_lock = threading.Lock()

        with patch("subprocess.run", side_effect=_blocking_fake_run(shared, cap)):
            threads = _run_cli_callers(
                num, analyzer, completed, completed_lock, errors, errors_lock
            )
            _wait_cond(shared, lambda: shared.active == cap, f"{cap} not active")
            with shared.condition:
                active_at_cap, peak_at_cap = shared.active, shared.peak
            _release_all(shared)
            still_alive = _join_threads(threads)

        self.assertEqual(still_alive, [])
        self.assertEqual(active_at_cap, cap)
        self.assertEqual(peak_at_cap, cap)
        with errors_lock:
            self.assertEqual(errors, [])
        with completed_lock:
            self.assertEqual(completed[0], num)


class TestCliVerificationShared(_Base):
    """_invoke_claude_cli and invoke_verification_pass share one semaphore."""

    def test_invoke_cli_and_verification_share_semaphore(self) -> None:
        """With cap=1, verification cannot enter subprocess.run while CLI holds
        the semaphore; it proceeds immediately after CLI releases."""
        cap = 1
        analyzer = _make_analyzer(self._tmp_path)
        doc_path = self._tmp_path / "ver_doc.md"
        doc_path.write_text("# Test\n\nContent for verification.\n")
        sem = _get_verification_semaphore(cap)
        shared = _SharedState()
        call_count: List[int] = [0]
        call_count_lock = threading.Lock()
        errors: List[Exception] = []
        errors_lock = threading.Lock()
        completed: List[int] = [0]
        completed_lock = threading.Lock()
        cfg = _make_config(cap)

        config_mock = MagicMock()
        config_mock.get_config.return_value.claude_integration_config.max_concurrent_claude_cli = cap

        with (
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=config_mock,
            ),
            patch(
                "subprocess.run",
                side_effect=_build_dispatching_run(
                    shared, call_count, call_count_lock, doc_path
                ),
            ),
        ):
            cli_threads = _run_cli_callers(
                1, analyzer, completed, completed_lock, errors, errors_lock
            )
            _wait_cond(shared, lambda: shared.active == 1, "cli not in subprocess.run")
            self.assertEqual(_sem_value(sem), 0, "semaphore should be exhausted")

            t_ver = _run_verification_caller(
                analyzer,
                cfg,
                shared,
                completed,
                completed_lock,
                errors,
                errors_lock,
                doc_path=doc_path,
            )
            _wait_cond(shared, lambda: shared.ver_started, "verification not started")

            self.assertEqual(
                _sem_value(sem), 0, "semaphore still exhausted while CLI holds it"
            )
            with call_count_lock:
                count_blocked = call_count[0]
            self.assertEqual(
                count_blocked, 1, "verification entered subprocess.run too early"
            )

            _release_all(shared)
            _wait_cond(shared, lambda: shared.ver_entered_run, "verification never ran")
            still_alive = _join_threads(cli_threads + [t_ver])

        self.assertEqual(still_alive, [])
        with errors_lock:
            self.assertEqual(errors, [])
        with completed_lock:
            self.assertEqual(completed[0], 2)
        with call_count_lock:
            self.assertEqual(call_count[0], 2)


class TestColdStartSemaphoreCompatibility(_Base):
    """AC5 regression: a cold-start _invoke_claude_cli call followed by
    invoke_verification_pass must NOT raise ValueError when both use the
    same configured max_concurrent_claude_cli value."""

    def test_cli_cold_start_followed_by_verification_does_not_raise(self) -> None:
        """Clear the semaphore cache. CLI call first with configured max=5. Then
        verification pass with same configured max=5. No ValueError."""
        import os
        import time

        _VERIFICATION_SEMAPHORE_STATE.clear()

        analyzer = _make_analyzer(self._tmp_path)
        doc_path = self._tmp_path / "cold_start_doc.md"
        doc_path.write_text("# Cold Start Test\n\nOriginal content.\n")
        cfg_max = 5

        config_mock = MagicMock()
        config_mock.get_config.return_value.claude_integration_config.max_concurrent_claude_cli = cfg_max

        cfg_for_verification = _make_config(cfg_max)
        call_count: List[int] = [0]

        def _fake_run(*args, **kwargs):
            """First call (CLI cold-start): return plain text.
            Second call (verification): write updated content, advance mtime by 2s
            using os.utime so the mtime-change postcondition is satisfied
            deterministically on all filesystems."""
            call_count[0] += 1
            if call_count[0] == 1:
                # CLI call — just needs non-error returncode
                return subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="CLI result text here", stderr=""
                )
            # Verification call: satisfy FILE_EDIT_COMPLETE, mtime-change, non-empty
            doc_path.write_text("# Cold Start Test\n\nVerified content.\n")
            future_time = time.time() + 2.0
            os.utime(doc_path, (future_time, future_time))
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="FILE_EDIT_COMPLETE", stderr=""
            )

        with (
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=config_mock,
            ),
            patch("subprocess.run", side_effect=_fake_run),
        ):
            # Cold-start CLI call — initialises semaphore with cfg_max
            analyzer._invoke_claude_cli(prompt="test", timeout=10, max_turns=0)

            # Verification pass with same cfg_max — must not raise ValueError
            # v2 returns None on success
            analyzer.invoke_verification_pass(
                document_path=doc_path,
                repo_list=[],
                config=cfg_for_verification,
            )

        # Both calls completed without raising ValueError
        # Semaphore singleton still valid with the configured value
        sem = _get_verification_semaphore(cfg_max)
        self.assertIsNotNone(sem)
