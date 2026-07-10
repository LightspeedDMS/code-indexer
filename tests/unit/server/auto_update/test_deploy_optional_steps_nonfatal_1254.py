"""
Tests for Bug #1254: Auto-updater aborts deploy when OPTIONAL infra-setup
steps fail on immutable/read-only hosts (swap fallocate -> DEPLOY-GENERAL-093
fatal); optional steps must be non-fatal so core deploy+restart completes.

Root cause: `_ensure_swap_file()` (a best-effort "OOM safety net" per its own
docstring) logged ERROR and returned False on ANY fallocate/chmod/mkswap/
swapon subprocess failure. On a host with a read-only "/" (immutable host),
`/swapfile` can never be created, so the swap step always fails there.

AUDIT FINDING (full table in the PR/report): direct code reading of
execute() proves its SOLE call site for `_ensure_swap_file()` (Step 10) --
and likewise for `_ensure_memory_overcommit()` (Step 9) and
`_ensure_malloc_arena_max()` (Step 6.6) -- was ALREADY non-fatal before this
fix: `if not self._ensure_X(): logger.warning(...)` with NO `return False`
afterward, so execute() always falls through to the next step regardless of
these three functions' return values. The ONLY `return False` points inside
execute() that actually abort the deploy are: git pull (Step 1), the
self-restart smoke-test guard (Step 1.1, a code-safety gate for the
auto-updater's OWN code, not an immutable-host tuning step), the hnswlib
build (Step 1.6), pip install (Step 2), and the Rust toolchain / xray-cli
build (Step 16, Story #1024, deliberately FATAL by design and explicitly
left untouched here -- it is a feature-build gate, not infra-tuning, so
reclassifying it is out of this bug's stated scope; see the report for the
finding that it ALSO writes to a root path (/opt/rust) and may be the
*actual* remaining blocker on the observed immutable host).

Fix: `_ensure_swap_file()` now logs WARNING (not ERROR) and returns True
(not False) on every swap-setup subprocess failure (fallocate, chmod,
mkswap, swapon) AND on the generic exception handler -- "swap is an OOM
optimization, the server runs correctly without it" (the bug's own words).
This corrects the function's OWN internal contract (log level + return
value) for correctness/consistency and defense-in-depth, even though the
existing call site already tolerated a False return. The "swap already
active" early-return and the pre-existing non-fatal fstab-append warning
path are unchanged.

Mocking strategy: subprocess.run is the external boundary mocked for the
direct _ensure_swap_file() unit tests. For execute()-level tests, every
OTHER step is patched to a no-op success via patch.object() (established
pattern from test_deploy_nosudo_tmpdir_1251.py::_NOOP_EXECUTE_STEPS and
test_ensure_codex_cli_installed_845.py::_patch_execute_siblings) so only the
step under test actually drives execute()'s control flow.
"""

import logging
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def executor() -> DeploymentExecutor:
    """DeploymentExecutor instance under test."""
    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        branch="master",
        service_name="cidx-server",
    )


def _ok(stdout: str = "", stderr: str = "") -> MagicMock:
    """Simulate a successful subprocess.run() result."""
    return MagicMock(returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr: str) -> MagicMock:
    """Simulate a failed subprocess.run() result."""
    return MagicMock(returncode=1, stdout="", stderr=stderr)


READ_ONLY_FS_STDERR = "cannot open /swapfile: Read-only file system"


# ---------------------------------------------------------------------------
# TestEnsureSwapFileBestEffort -- direct unit tests of _ensure_swap_file()
# ---------------------------------------------------------------------------


class TestEnsureSwapFileBestEffort:
    """_ensure_swap_file() must NEVER return False -- it is a best-effort OOM
    safety net (Bug #1254). Every subprocess failure must log WARNING (not
    ERROR) and return True.
    """

    def test_fallocate_failure_returns_true_and_logs_warning(
        self, executor: DeploymentExecutor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """fallocate failure (e.g. read-only filesystem) must be non-fatal.

        RED against pre-fix code: the current implementation logs ERROR and
        returns False on fallocate failure (deployment_executor.py ~3246-3254).
        This is the EXACT failure observed live on staging v11.12.0
        ([DEPLOY-GENERAL-093] fallocate -l 4G /swapfile failed: cannot open
        /swapfile: Read-only file system).
        """
        calls = [
            _ok(stdout=""),  # swapon --show --noheadings: no swap active
            _fail(READ_ONLY_FS_STDERR),  # sudo fallocate -l 4G /swapfile
        ]
        with (
            patch("subprocess.run", side_effect=calls),
            caplog.at_level(logging.WARNING),
        ):
            result = executor._ensure_swap_file()

        assert result is True, (
            "fallocate failure on a read-only host must be non-fatal "
            "(swap is an OOM optimization, not a correctness requirement)"
        )
        assert not any(r.levelno >= logging.ERROR for r in caplog.records), (
            f"No ERROR-level log expected for a best-effort failure; got: "
            f"{[(r.levelname, r.message) for r in caplog.records]}"
        )
        assert any(
            r.levelno == logging.WARNING and "fallocate" in r.message
            for r in caplog.records
        ), "Expected a WARNING log mentioning fallocate"

    def test_chmod_failure_returns_true_and_logs_warning(
        self, executor: DeploymentExecutor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """chmod 600 /swapfile failure must be non-fatal."""
        calls = [
            _ok(stdout=""),
            _ok(),  # fallocate succeeds
            _fail("chmod: changing permissions of '/swapfile': Read-only file system"),
        ]
        with (
            patch("subprocess.run", side_effect=calls),
            caplog.at_level(logging.WARNING),
        ):
            result = executor._ensure_swap_file()

        assert result is True, "chmod failure must be non-fatal"
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
        assert any(
            r.levelno == logging.WARNING and "chmod" in r.message
            for r in caplog.records
        )

    def test_mkswap_failure_returns_true_and_logs_warning(
        self, executor: DeploymentExecutor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """mkswap /swapfile failure must be non-fatal."""
        calls = [
            _ok(stdout=""),
            _ok(),  # fallocate
            _ok(),  # chmod
            _fail("mkswap: cannot open /swapfile: Read-only file system"),
        ]
        with (
            patch("subprocess.run", side_effect=calls),
            caplog.at_level(logging.WARNING),
        ):
            result = executor._ensure_swap_file()

        assert result is True, "mkswap failure must be non-fatal"
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
        assert any(
            r.levelno == logging.WARNING and "mkswap" in r.message
            for r in caplog.records
        )

    def test_swapon_failure_returns_true_and_logs_warning(
        self, executor: DeploymentExecutor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """swapon /swapfile failure must be non-fatal."""
        calls = [
            _ok(stdout=""),
            _ok(),  # fallocate
            _ok(),  # chmod
            _ok(),  # mkswap
            _fail("swapon: /swapfile: swapon failed: Invalid argument"),
        ]
        with (
            patch("subprocess.run", side_effect=calls),
            caplog.at_level(logging.WARNING),
        ):
            result = executor._ensure_swap_file()

        assert result is True, "swapon failure must be non-fatal"
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
        assert any(
            r.levelno == logging.WARNING and "swapon" in r.message
            for r in caplog.records
        )

    def test_generic_exception_returns_true_and_logs_warning(
        self, executor: DeploymentExecutor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Any unexpected exception during swap setup must be non-fatal."""
        with (
            patch("subprocess.run", side_effect=OSError("swapon: command not found")),
            caplog.at_level(logging.WARNING),
        ):
            result = executor._ensure_swap_file()

        assert result is True, "Unexpected exceptions must be non-fatal"
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_swap_already_active_returns_true_without_fallocate(
        self, executor: DeploymentExecutor
    ) -> None:
        """Regression: the 'swap already active' fast-path is unchanged --
        no fallocate/chmod/mkswap/swapon attempted at all.
        """
        calls: list = []

        def dispatch(cmd: list, **_kw: object) -> MagicMock:
            calls.append(list(cmd))
            return _ok(stdout="/swapfile file 4194300 0 -2\n")

        with patch("subprocess.run", side_effect=dispatch):
            result = executor._ensure_swap_file()

        assert result is True
        assert len(calls) == 1, (
            f"Only the swapon --show check should run when swap is already "
            f"active; got {calls}"
        )

    def test_success_path_still_returns_true(
        self, executor: DeploymentExecutor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression: full success path (no existing swap, all 4 steps
        succeed, fstab already has the entry) still returns True and logs
        no WARNING/ERROR.
        """
        calls = [
            _ok(stdout=""),  # swapon --show: no swap active
            _ok(),  # fallocate
            _ok(),  # chmod
            _ok(),  # mkswap
            _ok(),  # swapon
            _ok(stdout="/swapfile none swap sw 0 0\n"),  # cat /etc/fstab: entry present
        ]
        with (
            patch("subprocess.run", side_effect=calls),
            caplog.at_level(logging.WARNING),
        ):
            result = executor._ensure_swap_file()

        assert result is True
        assert not any(r.levelno >= logging.WARNING for r in caplog.records), (
            "Full success must not log any WARNING/ERROR"
        )

    def test_fstab_append_failure_remains_non_fatal_warning(
        self, executor: DeploymentExecutor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression: the pre-existing non-fatal fstab-append path (Bug
        #1254 does not touch this -- it was already WARNING + return True).
        """
        calls = [
            _ok(stdout=""),  # swapon --show: no swap active
            _ok(),  # fallocate
            _ok(),  # chmod
            _ok(),  # mkswap
            _ok(),  # swapon
            _ok(stdout=""),  # cat /etc/fstab: no entry present
            _fail("tee: /etc/fstab: Read-only file system"),  # tee -a /etc/fstab
        ]
        with (
            patch("subprocess.run", side_effect=calls),
            caplog.at_level(logging.WARNING),
        ):
            result = executor._ensure_swap_file()

        assert result is True
        assert any(
            r.levelno == logging.WARNING and "fstab" in r.message
            for r in caplog.records
        )
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# execute() siblings helper -- mirrors the established pattern from
# test_deploy_nosudo_tmpdir_1251.py::_NOOP_EXECUTE_STEPS and
# test_ensure_codex_cli_installed_845.py::_patch_execute_siblings
# ---------------------------------------------------------------------------

_NOOP_EXECUTE_STEPS: dict = {
    "git_pull": True,
    "git_submodule_update": True,
    "_build_hnswlib_with_fallback": True,
    "pip_install": True,
    "_ensure_launch_config": None,
    "_ensure_cidx_repo_root": True,
    "_ensure_git_safe_directory": True,
    "_ensure_auto_updater_uses_server_python": True,
    "_ensure_data_dir_env_var": True,
    "_ensure_malloc_arena_max": True,
    "_ensure_codex_cli_installed": True,
    "ensure_ripgrep": True,
    "_ensure_sudoers_restart": True,
    "_ensure_memory_overcommit": True,
    "_ensure_swap_file": True,
    "_ensure_claude_cli_updated": True,
    "_ensure_pace_maker_installed": True,
    "_ensure_claude_cli_installed": True,
    "_ensure_nfs_research_symlinks": True,
    "_ensure_activated_repos_symlink_for_cow_daemon": True,
    "_ensure_daemon_storage_path": True,
    "_ensure_systemd_claude_path": True,
    "_ensure_rust_toolchain": True,
    "_calculate_auto_update_hash": "fixed-hash-1254",
}


def _patched_execute(executor: DeploymentExecutor, overrides: dict) -> ExitStack:
    """Patch every execute() step to a no-op success, except for the methods
    named in `overrides` (method_name -> return_value). Caller must enter
    the returned ExitStack via `with` and perform both the execute() call
    AND any post-call mock assertions INSIDE that `with` block (the patches
    are reverted on __exit__).
    """
    stack = ExitStack()
    merged = dict(_NOOP_EXECUTE_STEPS)
    merged.update(overrides)
    for method_name, value in merged.items():
        stack.enter_context(patch.object(executor, method_name, return_value=value))
    return stack


# ---------------------------------------------------------------------------
# TestExecuteProceedsPastOptionalStepFailures
# ---------------------------------------------------------------------------


class TestExecuteProceedsPastOptionalStepFailures:
    """execute() must proceed all the way to a successful return when an
    OPTIONAL infra-tuning step (Step 9 memory overcommit, Step 10 swap,
    Step 6.6 malloc_arena_max) fails -- the deploy must reach the final
    success return so the caller (run_once.py) proceeds to restart_server().
    """

    def test_execute_succeeds_when_ensure_swap_file_fails(
        self, executor: DeploymentExecutor
    ) -> None:
        """execute() must still return True (and reach the Rust-toolchain /
        final step) when _ensure_swap_file() reports failure.

        Locks in the audit finding: Step 10's call site in execute() was
        ALREADY non-fatal before this fix (logs WARNING, never returns
        False) -- this proves it end-to-end and guards against a future
        regression that re-adds an abort here.
        """
        with _patched_execute(executor, {"_ensure_swap_file": False}):
            result = executor.execute()

            assert result is True, (
                "execute() must proceed to a successful completion even "
                "when the swap-file safety net could not be created"
            )
            # Prove execute() did not short-circuit early: the LAST
            # pre-restart step (Rust toolchain, Step 16) must still run.
            executor._ensure_rust_toolchain.assert_called_once()  # type: ignore[attr-defined]

    def test_execute_succeeds_when_ensure_memory_overcommit_fails(
        self, executor: DeploymentExecutor
    ) -> None:
        """Regression: Step 9 (memory overcommit) call site is already
        non-fatal -- execute() proceeds past a failure unchanged.
        """
        with _patched_execute(executor, {"_ensure_memory_overcommit": False}):
            result = executor.execute()

            assert result is True
            executor._ensure_rust_toolchain.assert_called_once()  # type: ignore[attr-defined]

    def test_execute_succeeds_when_ensure_malloc_arena_max_fails(
        self, executor: DeploymentExecutor
    ) -> None:
        """Regression: malloc_arena_max call site is already non-fatal --
        execute() proceeds past a failure unchanged.
        """
        with _patched_execute(executor, {"_ensure_malloc_arena_max": False}):
            result = executor.execute()

            assert result is True
            executor._ensure_rust_toolchain.assert_called_once()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# TestExecuteCoreStepsRemainFatal
# ---------------------------------------------------------------------------


class TestExecuteCoreStepsRemainFatal:
    """Regression: CORE steps (git pull, hnswlib build, pip install) must
    STILL abort execute() on failure -- Bug #1254's fix must not weaken
    these. The Rust toolchain step is also proven to remain fatal: it is a
    deliberate Story #1024 feature-build gate, not an immutable-host
    infra-tuning step, and reclassifying it is explicitly out of scope.
    """

    def test_execute_fails_when_git_pull_fails(
        self, executor: DeploymentExecutor
    ) -> None:
        with _patched_execute(executor, {"git_pull": False}):
            result = executor.execute()

            assert result is False, "git pull failure MUST still abort execute()"

    def test_execute_fails_when_hnswlib_build_fails(
        self, executor: DeploymentExecutor
    ) -> None:
        with _patched_execute(executor, {"_build_hnswlib_with_fallback": False}):
            result = executor.execute()

            assert result is False, "hnswlib build failure MUST still abort execute()"

    def test_execute_fails_when_pip_install_fails(
        self, executor: DeploymentExecutor
    ) -> None:
        with _patched_execute(executor, {"pip_install": False}):
            result = executor.execute()

            assert result is False, "pip install failure MUST still abort execute()"

    def test_execute_fails_when_rust_toolchain_fails(
        self, executor: DeploymentExecutor
    ) -> None:
        """Audit finding: Rust toolchain (Story #1024, xray-cli build) is
        deliberately left FATAL by this fix -- it is a feature-build gate,
        not an immutable-host infra-tuning step, and is out of Bug #1254's
        explicit scope. This test locks in that this fix did NOT broaden
        non-fatal treatment to it.
        """
        with _patched_execute(executor, {"_ensure_rust_toolchain": False}):
            result = executor.execute()

            assert result is False, (
                "Rust toolchain failure MUST still abort execute() -- "
                "intentionally left fatal, out of Bug #1254 scope"
            )
