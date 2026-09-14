"""S0a Phase 2 timeout / cancellation regression floor (story #1789).

Pins what the engine reports, and what it does to the child process, when a
Phase 2 evaluator outruns its budget:

  * the terminal status of a real Phase 2 timeout
  * a real SIGKILL of the real xray-cli subprocess, with no orphan or zombie
  * that a Phase 1 driver failure and a Phase 2 timeout stay distinguishable

Anti-mock posture: nothing is mocked, stubbed or replaced. The timeout is
provoked by a real Rust evaluator that is genuinely too slow -- compiled by
real rustc and executed by the real ``xray-cli`` subprocess -- and the kill is
a real signal delivered to a real process.

Two deliberate scope boundaries, both reported rather than silently skipped:

* The Phase 1 TIMEOUT terminal status is not re-tested here. It is already
  covered against a real on-disk tree by
  tests/unit/xray/test_phase1_timeout_1598.py
  (``TestFilenameModeTimeout.test_timeout_during_traversal_reports_partial``
  asserts partial/timeout/files_processed==0/matches==[]). Reproducing it in
  a fast unit test needs either a multi-gigabyte corpus or a patch of the
  engine's own walk, and patching the system under test is not acceptable.
  The distinctness test below therefore contrasts the Phase 2 timeout with a
  live Phase 1 DRIVER FAILURE, which needs no patching at all.

* Temp-file cleanup after a timeout is not asserted here. Attributing an
  ``xray_eval_*`` / ``xray_files_*`` artifact to one invocation requires
  either mutating the ``tempfile.tempdir`` global or an injectable
  temp-directory seam in ``RustNativeBackend._write_temp_file``, which has
  none. Verified manually instead; see the story report.

Cap and regex-driver coverage lives in ``test_s0a_engine_caps_1789.py``.
"""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from tests.unit.xray.s0a_1789_support import (
    PHASE2_TIMEOUT_SECONDS,
    RUST_EVALUATOR_BOUNDED_SLOW,
    run_engine,
    write_java_files,
)


@pytest.fixture
def s0a_search_engine():
    """Instantiate a real XRaySearchEngine, skipping if extras are absent."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


@pytest.fixture
def s0a_requires_xray_cli():
    """Skip when the compiled xray-cli binary is not present.

    Phase 2 is a real subprocess; without the binary these tests would assert
    against a BinaryNotFound error instead of the behaviour under test.
    """
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    if not backend._xray_cli_path.exists():
        pytest.skip(
            f"xray-cli not built at {backend._xray_cli_path}; "
            "run: cd rust && cargo build --release"
        )


def _run_phase2_timeout(engine: Any, root: Path, **kwargs: Any) -> Dict[str, Any]:
    """Drive a real Phase 2 evaluator timeout through the xray-cli subprocess.

    `engine` is typed `Any` and the overrides stay `**kwargs` deliberately:
    this is a thin pass-through to ``run_engine`` and thence to
    ``XRaySearchEngine.run``, whose keyword surface is large and differs per
    test. Re-declaring that signature here would duplicate the production one
    and silently drift from it, so ``run()`` remains the single authority on
    what is accepted -- including validating its own values, which
    TestTimeoutValidation asserts directly.

    Raises:
        TypeError: if `root` is not a Path.
    """
    if not isinstance(root, Path):
        raise TypeError(f"root must be a Path, got {type(root).__name__}")

    write_java_files(root, 1)
    return run_engine(
        engine,
        root,
        evaluator_code=RUST_EVALUATOR_BOUNDED_SLOW,
        timeout_seconds=PHASE2_TIMEOUT_SECONDS,
        **kwargs,
    )


class TestPhase2TimeoutStatus:
    def test_reports_partial_timeout_with_a_timeout_error(
        self, s0a_search_engine, tmp_path, s0a_requires_xray_cli
    ):
        result = _run_phase2_timeout(s0a_search_engine, tmp_path)

        assert result["partial"] is True
        assert result["timeout"] is True
        assert result["matches"] == []
        # Phase 1 succeeded, so the candidate IS counted -- the engine knows
        # exactly how many files it meant to evaluate, and that it did none.
        assert result["files_total"] == 1
        assert result["files_processed"] == 0
        # Phase 2 reached the evaluator, so it reports WHY nothing came back.
        assert result["evaluation_errors"], "a Phase 2 timeout must explain itself"
        assert any(
            "timed out" in str(err.get("error_message", ""))
            for err in result["evaluation_errors"]
        ), f"expected a timeout error, got {result['evaluation_errors']}"

    def test_sigkills_and_reaps_the_xray_cli_process(
        self, s0a_search_engine, tmp_path, s0a_requires_xray_cli
    ):
        # on_process_spawned receives the backend's real subprocess handle.
        spawned: List[subprocess.Popen] = []

        result = _run_phase2_timeout(
            s0a_search_engine, tmp_path, on_process_spawned=spawned.append
        )

        assert result["timeout"] is True
        assert len(spawned) == 1, "expected exactly one xray-cli invocation"
        proc = spawned[0]
        # poll() returns None while the child is still running, so a non-None
        # returncode proves it was both terminated AND reaped -- no orphan and
        # no zombie left behind.
        assert proc.poll() is not None, "xray-cli was left running (orphan process)"
        # POSIX reports a signal death as the negated signal number. Asserting
        # the exact signal distinguishes the backend's deliberate SIGKILL from
        # a SIGTERM or an ordinary non-zero exit, either of which would also
        # satisfy a bare "returncode != 0".
        assert proc.returncode == -signal.SIGKILL, (
            "the backend must SIGKILL a timed-out xray-cli, "
            f"got returncode={proc.returncode}"
        )


class TestTerminalStatusDistinctness:
    def test_phase1_failure_and_phase2_timeout_are_distinguishable(
        self, s0a_search_engine, tmp_path, s0a_requires_xray_cli
    ):
        # Two genuinely live terminal statuses, both "partial", which a caller
        # must be able to tell apart in order to react correctly.
        phase1_repo = tmp_path / "phase1"
        phase1_repo.mkdir()
        write_java_files(phase1_repo, 2)
        phase1 = run_engine(s0a_search_engine, phase1_repo, driver_regex="[unclosed")

        phase2_repo = tmp_path / "phase2"
        phase2_repo.mkdir()
        phase2 = _run_phase2_timeout(s0a_search_engine, phase2_repo)

        assert phase1["partial"] is True and phase2["partial"] is True

        # A Phase 1 driver failure: flagged phase1_failed, carries the
        # driver's own error text, is NOT a timeout, and never reached the
        # evaluator so it reports no evaluation errors.
        assert phase1["phase1_failed"] is True
        assert "timeout" not in phase1
        assert phase1["evaluation_errors"] == []

        # A Phase 2 timeout: flagged timeout, has no phase1_failed, and DOES
        # carry an evaluation error explaining the outcome.
        assert phase2["timeout"] is True
        assert "phase1_failed" not in phase2
        assert phase2["evaluation_errors"] != []


class TestTimeoutValidation:
    @pytest.mark.parametrize("bad_timeout", [0, -1], ids=["zero", "negative"])
    def test_non_positive_timeout_is_rejected(
        self, s0a_search_engine, tmp_path, bad_timeout
    ):
        # The documented guard is `timeout_seconds <= 0`, so both halves of
        # that condition must be exercised, not just the zero boundary.
        write_java_files(tmp_path, 1)
        with pytest.raises(ValueError, match="timeout_seconds must be > 0"):
            run_engine(s0a_search_engine, tmp_path, timeout_seconds=bad_timeout)
