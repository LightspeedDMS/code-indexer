"""Story #1200 AC3/AC4/AC5/FIX-6: check_pending_launch_restart() behavior.

RED -> GREEN -> REFACTOR.

AC3  -- check_pending_launch_restart() wired into _poll_loop (NOT a callback).
AC3  -- per-poll retry while target > applied; materialize-before-signal; no applied write.
AC4  -- no-op when applied >= target.
AC5  -- absent applied_launch.json -> COALESCE 0 -> signal if target > 0.
FIX-6 -- ONE WARNING after >PENDING_RESTART_WARN_THRESHOLD polls; reset on convergence.

All tests use real SQLite and real filesystem paths (not SUT method mocks).
External constants (RESTART_SIGNAL_PATH, APPLIED_LAUNCH_CONFIG_PATH, LAUNCH_CONFIG_PATH)
are patched at the module level so real production code runs against temp paths.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Named constants for source-scan windows
# ---------------------------------------------------------------------------

_METHOD_SCAN_WINDOW = 4500

# Marker for the statement that immediately follows the `_poll_loop` closure
# in config_service.py (the thread is created right after the closure body
# ends). Used to bound the `_poll_loop` body extraction precisely instead of
# relying on a fixed character-count window, which is brittle to legitimate
# additions inside the closure (see Bug #1249).
_POLL_LOOP_END_MARKER = "self._reload_thread = threading.Thread("

# ---------------------------------------------------------------------------
# Source path
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONFIG_SERVICE_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "services" / "config_service.py"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sqlite_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config ("
            "config_key TEXT PRIMARY KEY DEFAULT 'runtime', "
            "config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT DEFAULT (datetime('now')), "
            "updated_by TEXT)"
        )
        conn.commit()


def _seed_runtime_row(db_path: str, data: dict, version: int = 1) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO server_config (config_key, config_json, version, updated_by) "
            "VALUES ('runtime', ?, ?, 'test') "
            "ON CONFLICT(config_key) DO UPDATE SET "
            "config_json = excluded.config_json, "
            "version = excluded.version",
            (json.dumps(data), version),
        )
        conn.commit()


def _write_applied_launch(path: Path, applied_gen: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "workers": 1,
                "log_level": "INFO",
                "host": "127.0.0.1",
                "port": 8000,
                "applied_restart_generation": applied_gen,
            }
        )
    )


@pytest.fixture()
def pending_env(tmp_path: Path):
    """Fixture: real SQLite svc + patched launch paths in a temp dir.

    Returns a namespace with:
        svc          -- ConfigService backed by real SQLite
        db_path      -- path to the SQLite file
        signal_path  -- Path where restart.signal will be written
        applied_path -- Path for applied_launch.json
        launch_path  -- Path for launch.json
    """
    from code_indexer.server.services.config_service import ConfigService

    db_path = str(tmp_path / "cidx.db")
    _make_sqlite_db(db_path)
    signal_path = tmp_path / "restart.signal"
    applied_path = tmp_path / "applied_launch.json"
    launch_path = tmp_path / "launch.json"

    svc = ConfigService(server_dir_path=str(tmp_path))
    svc.load_config()
    svc._sqlite_db_path = db_path

    class _Env:
        pass

    env = _Env()
    env.svc = svc  # type: ignore[attr-defined]
    env.db_path = db_path  # type: ignore[attr-defined]
    env.signal_path = signal_path  # type: ignore[attr-defined]
    env.applied_path = applied_path  # type: ignore[attr-defined]
    env.launch_path = launch_path  # type: ignore[attr-defined]
    return env


@contextmanager
def _patch_paths(env):
    """Patch module-level path constants to temp dir equivalents."""
    mod = "code_indexer.server.services.config_service"
    with (
        patch(f"{mod}.RESTART_SIGNAL_PATH", env.signal_path),
        patch(f"{mod}.APPLIED_LAUNCH_CONFIG_PATH", env.applied_path),
        patch(f"{mod}.LAUNCH_CONFIG_PATH", env.launch_path),
    ):
        yield


# ===========================================================================
# AC3: wiring guards (source-text / source-order)
# ===========================================================================


class TestCheckPendingLaunchRestartWiring:
    """AC3: source-order guards -- check is in _poll_loop, NOT a callback."""

    def test_method_defined_in_config_service(self) -> None:
        source = _CONFIG_SERVICE_PATH.read_text()
        assert "def check_pending_launch_restart" in source, (
            "AC3: ConfigService must define check_pending_launch_restart()"
        )

    def test_wired_into_poll_loop_not_callbacks(self) -> None:
        source = _CONFIG_SERVICE_PATH.read_text()
        poll_start = source.find("def _poll_loop(")
        assert poll_start != -1, "_poll_loop function not found"
        # Bound the extraction to the actual `_poll_loop` closure body: it
        # ends right before the statement that spawns the thread for it.
        # Falling back to end-of-file (rather than a fixed char window) on a
        # malformed/missing marker keeps the assertion able to genuinely
        # FAIL if check_pending_launch_restart is ever removed from the loop.
        poll_end = source.find(_POLL_LOOP_END_MARKER, poll_start)
        if poll_end == -1:
            poll_end = len(source)
        poll_body = source[poll_start:poll_end]
        assert "check_pending_launch_restart" in poll_body, (
            "AC3 CRITICAL-C1: check_pending_launch_restart must be called "
            "inside _poll_loop, NOT registered as a callback"
        )

    def test_not_registered_as_callback(self) -> None:
        source = _CONFIG_SERVICE_PATH.read_text()
        callback_calls = re.findall(r"register_on_change_callback\s*\([^)]+\)", source)
        for call_site in callback_calls:
            assert "check_pending_launch_restart" not in call_site, (
                "AC3 CRITICAL-C1: check_pending_launch_restart must NOT be "
                "registered as an on-change callback"
            )

    def test_materialize_before_signal_in_source_order(self) -> None:
        """AC3: the materialize call must precede the restart-signal write in source order.

        Searches for code-only patterns (assignment result and the path method call)
        to avoid matching docstring text.
        """
        source = _CONFIG_SERVICE_PATH.read_text()
        check_start = source.find("def check_pending_launch_restart")
        assert check_start != -1
        method_body = source[check_start : check_start + _METHOD_SCAN_WINDOW]
        # Find the actual materialize call (result assigned to a variable)
        mat_pos = method_body.find("= self.materialize_launch_config()")
        # Find the actual signal path filesystem access (mkdir before write_text).
        # Split the token so this test's source text does not self-match.
        signal_write_token = "RESTART_SIGNAL" + "_PATH.parent.mkdir"
        sig_pos = method_body.find(signal_write_token)
        assert mat_pos != -1, (
            "materialize_launch_config() call (assigned form) must appear in method body"
        )
        assert sig_pos != -1, "restart signal path mkdir must appear in method body"
        assert mat_pos < sig_pos, (
            "AC3: materialize_launch_config() must be called BEFORE writing the "
            "restart signal in check_pending_launch_restart"
        )

    def test_check_does_not_write_applied_launch_in_source(self) -> None:
        """AC3: check_pending_launch_restart must not perform write ops on the applied path.

        Verifies absence of write operations by searching for the combined path-write
        pattern using a split token so the search string itself does not match.
        """
        source = _CONFIG_SERVICE_PATH.read_text()
        check_start = source.find("def check_pending_launch_restart")
        assert check_start != -1
        method_body = source[check_start : check_start + _METHOD_SCAN_WINDOW]
        # Split token: APPLIED_LAUNCH + CONFIG_PATH.write — avoids self-match in docstrings
        write_token = "APPLIED_LAUNCH" + "_CONFIG_PATH.write"
        assert write_token not in method_body, (
            "AC3: check_pending_launch_restart must NOT write to the applied launch path"
        )

    def test_module_defines_pending_restart_warn_threshold(self) -> None:
        """FIX-6: config_service must define PENDING_RESTART_WARN_THRESHOLD."""
        source = _CONFIG_SERVICE_PATH.read_text()
        assert "PENDING_RESTART_WARN_THRESHOLD" in source, (
            "FIX-6: config_service.py must define PENDING_RESTART_WARN_THRESHOLD"
        )


# ===========================================================================
# AC3/AC4/AC5: behavioral tests (real SQLite + real filesystem)
# ===========================================================================


@pytest.mark.slow
class TestCheckPendingLaunchRestartBehavioral:
    """AC3/AC4/AC5: per-poll behavior with real SQLite and real filesystem."""

    def test_signals_restart_when_target_gt_applied(self, pending_env) -> None:
        """target > applied -> materialize launch.json then write restart.signal."""
        _seed_runtime_row(
            pending_env.db_path,
            {"workers": 1, "launch_restart_generation": 2},
        )
        _write_applied_launch(pending_env.applied_path, applied_gen=1)

        with _patch_paths(pending_env):
            pending_env.svc.check_pending_launch_restart()

        assert pending_env.signal_path.exists(), (
            "AC3: RESTART_SIGNAL_PATH must be written when target(2) > applied(1)"
        )
        assert pending_env.launch_path.exists(), (
            "AC3: materialize_launch_config() must have run (wrote launch.json)"
        )

    def test_noop_when_applied_equals_target(self, pending_env) -> None:
        """applied == target -> no signal written."""
        _seed_runtime_row(
            pending_env.db_path,
            {"workers": 1, "launch_restart_generation": 3},
        )
        _write_applied_launch(pending_env.applied_path, applied_gen=3)

        with _patch_paths(pending_env):
            pending_env.svc.check_pending_launch_restart()

        assert not pending_env.signal_path.exists(), (
            "AC4: RESTART_SIGNAL_PATH must NOT be written when applied == target"
        )

    def test_noop_when_applied_greater_than_target(self, pending_env) -> None:
        """applied > target -> no-op (safety guard)."""
        _seed_runtime_row(
            pending_env.db_path,
            {"workers": 1, "launch_restart_generation": 2},
        )
        _write_applied_launch(pending_env.applied_path, applied_gen=5)

        with _patch_paths(pending_env):
            pending_env.svc.check_pending_launch_restart()

        assert not pending_env.signal_path.exists()

    def test_absent_applied_launch_coalesces_to_zero(self, pending_env) -> None:
        """AC5: absent applied_launch.json -> applied=0; target=1 -> signal written."""
        _seed_runtime_row(
            pending_env.db_path,
            {"workers": 1, "launch_restart_generation": 1},
        )
        # No applied_launch.json written

        with _patch_paths(pending_env):
            pending_env.svc.check_pending_launch_restart()

        assert pending_env.signal_path.exists(), (
            "AC5: absent applied_launch.json -> COALESCE applied=0; "
            "target=1 > applied=0 -> signal must be written"
        )

    def test_no_signal_when_materialize_fails(self, pending_env) -> None:
        """AC3: if materialize_launch_config() fails, must NOT write restart.signal.

        We make LAUNCH_CONFIG_PATH's parent non-writable to force materialize to fail.
        """
        _seed_runtime_row(
            pending_env.db_path,
            {"workers": 1, "launch_restart_generation": 2},
        )
        _write_applied_launch(pending_env.applied_path, applied_gen=1)

        # Point launch_path to a directory that cannot be created/written
        bad_launch_path = Path("/proc/cidx_no_write_permission/launch.json")

        mod = "code_indexer.server.services.config_service"
        with (
            patch(f"{mod}.RESTART_SIGNAL_PATH", pending_env.signal_path),
            patch(f"{mod}.APPLIED_LAUNCH_CONFIG_PATH", pending_env.applied_path),
            patch(f"{mod}.LAUNCH_CONFIG_PATH", bad_launch_path),
        ):
            pending_env.svc.check_pending_launch_restart()

        assert not pending_env.signal_path.exists(), (
            "AC3: restart.signal must NOT be written if materialize_launch_config fails"
        )

    def test_check_does_not_write_applied_launch_file(self, pending_env) -> None:
        """AC3: check_pending_launch_restart must NOT write applied_launch.json.

        Records applied file state before/after the call.
        """
        _seed_runtime_row(
            pending_env.db_path,
            {"workers": 1, "launch_restart_generation": 2},
        )
        _write_applied_launch(pending_env.applied_path, applied_gen=1)
        content_before = pending_env.applied_path.read_text()

        with _patch_paths(pending_env):
            pending_env.svc.check_pending_launch_restart()

        content_after = pending_env.applied_path.read_text()
        assert content_before == content_after, (
            "AC3: check_pending_launch_restart must NOT modify applied_launch.json; "
            "only the auto-updater (Story #1199) writes it"
        )

    def test_version_unchanged_still_signals(self, pending_env) -> None:
        """AC3/CRITICAL-C1: fires even when _db_config_version matches DB row version.

        Verifies independence from check_config_update().
        """
        _seed_runtime_row(
            pending_env.db_path,
            {"workers": 1, "launch_restart_generation": 1},
            version=5,
        )
        _write_applied_launch(pending_env.applied_path, applied_gen=0)
        pending_env.svc._db_config_version = 5  # version unchanged

        with _patch_paths(pending_env):
            pending_env.svc.check_pending_launch_restart()

        assert pending_env.signal_path.exists(), (
            "AC3: check_pending_launch_restart must fire even when version unchanged "
            "(CRITICAL-C1: version-diff-independence)"
        )


# ===========================================================================
# FIX-6: rate-limited WARNING after >PENDING_RESTART_WARN_THRESHOLD polls
# ===========================================================================


@pytest.mark.slow
class TestFix6StuckWarning:
    """FIX-6: rate-limited WARNING after threshold polls; reset on convergence."""

    def _threshold(self) -> int:
        from code_indexer.server.services import config_service as cs_module

        return int(getattr(cs_module, "PENDING_RESTART_WARN_THRESHOLD", 10))

    def _run_n_pending_polls(self, pending_env, n: int) -> None:
        """Run n consecutive pending polls (target > applied = 0)."""
        with _patch_paths(pending_env):
            for _ in range(n):
                pending_env.signal_path.unlink(missing_ok=True)
                pending_env.svc.check_pending_launch_restart()

    def test_warning_emitted_after_threshold(self, pending_env) -> None:
        """One WARNING logged when consecutive pending polls exceed threshold."""
        _seed_runtime_row(
            pending_env.db_path,
            {"workers": 1, "launch_restart_generation": 99},
        )
        # No applied_launch.json -> applied=0
        threshold = self._threshold()

        warning_count = 0

        def count_warning(*args, **kwargs):
            nonlocal warning_count
            warning_count += 1

        with patch("code_indexer.server.services.config_service.logger") as mock_logger:
            mock_logger.warning.side_effect = count_warning
            self._run_n_pending_polls(pending_env, threshold + 1)

        assert warning_count >= 1, (
            f"FIX-6: at least one WARNING must be logged after "
            f"{threshold + 1} consecutive pending polls"
        )

    def test_warning_rate_limited_not_per_poll(self, pending_env) -> None:
        """FIX-6: warning must fire exactly once, not every poll after threshold."""
        _seed_runtime_row(
            pending_env.db_path,
            {"workers": 1, "launch_restart_generation": 99},
        )
        threshold = self._threshold()
        warning_count = 0

        def count_warning(*args, **kwargs):
            nonlocal warning_count
            warning_count += 1

        with patch("code_indexer.server.services.config_service.logger") as mock_logger:
            mock_logger.warning.side_effect = count_warning
            self._run_n_pending_polls(pending_env, threshold * 3)

        assert warning_count == 1, (
            f"FIX-6: WARNING must fire exactly once (got {warning_count}), "
            "not once per poll after threshold"
        )

    def test_counter_resets_on_convergence(self, pending_env) -> None:
        """FIX-6: pending counter resets to 0 when applied catches up."""
        _seed_runtime_row(
            pending_env.db_path,
            {"workers": 1, "launch_restart_generation": 2},
        )
        threshold = self._threshold()

        with patch("code_indexer.server.services.config_service.logger"):
            self._run_n_pending_polls(pending_env, threshold + 2)
            # Converge: applied catches up to target=2
            _write_applied_launch(pending_env.applied_path, applied_gen=2)
            with _patch_paths(pending_env):
                pending_env.signal_path.unlink(missing_ok=True)
                pending_env.svc.check_pending_launch_restart()

        pending_counter = getattr(pending_env.svc, "_pending_restart_poll_count", -1)
        assert pending_counter == 0, (
            f"FIX-6: pending poll counter must reset to 0 on convergence "
            f"(got {pending_counter})"
        )
