"""Tests for Bug #1442: sync ALL CLI Python-environment dependencies during
deploy, not just hnswlib.

Production bug: `pip_install()` (the deploy step that runs `pip install -e .`
every cycle) targets ONLY `_get_server_python()` (the server's pipx venv).
The CLI's SEPARATE system-wide Python interpreter (resolved via
`_get_cli_python_interpreter()`, Bug #1392) is never re-synced after its
one-time `cidx-first-boot.sh` install, so it drifts: confirmed on production
via live `ModuleNotFoundError` reproduction for openpyxl, PIL, pyotp,
frontmatter, qrcode, tree_sitter_languages, langfuse -- packages the current
code (which the CLI still executes, being an editable install against the
same live repo checkout) now needs but were never installed there.

Fix: `_ensure_cli_dependencies_synced()` resolves the CLI's interpreter via
the EXISTING `_get_cli_python_interpreter()` (Bug #1392, not reimplemented),
then runs `pip install -e .` against it by reusing the SAME sudo /
--break-system-packages decision logic `pip_install()` already has, factored
into shared `_build_pip_install_cmd()` / `_run_pip_install_cmd()` helpers.
Non-fatal to the overall deploy (same rationale as
`_ensure_cli_hnswlib_capability()`): a failure in the CLI's independent
environment must never block the server's own restart/config steps.

Mocking strategy (established convention from
test_deployment_executor_hnswlib_cli_sync_1392.py's TestSkipGuardCapability
AwareRemediation): only TRUE external boundaries (shutil.which,
subprocess.run) are mocked. The REAL `_get_cli_python_interpreter()` shebang
resolution, `_is_user_install()` writability probe, and
`_pip_supports_break_system_packages()` probe all run for real against a
fake interpreter/entrypoint on disk and a fully-explicit subprocess dispatch
(an unmodeled command raises AssertionError rather than silently succeeding).
"""

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import code_indexer.server.auto_update.deployment_executor as _de_mod
from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


def _executor(tmp_path: Path) -> DeploymentExecutor:
    return DeploymentExecutor(
        repo_path=tmp_path,
        branch="master",
        service_name="cidx-server",
    )


@pytest.fixture()
def patched_data_dir(tmp_path: Path) -> Path:  # type: ignore[misc]
    """Patch _cidx_data_dir to tmp_path/.cidx-server (established convention)
    so filesystem ops stay confined to tmp_path."""
    data_dir = tmp_path / ".cidx-server"
    with patch.object(_de_mod, "_cidx_data_dir", data_dir):
        yield data_dir


def _setup_cli_entrypoint(tmp_path: Path) -> Path:
    """Create a real cidx entrypoint + interpreter file (established
    convention from test_deployment_executor_hnswlib_cli_sync_1392.py's
    TestGetCliPythonInterpreter.test_found_via_plain_shebang), so the REAL
    _get_cli_python_interpreter() shebang-resolution logic runs for real.

    Returns:
        The interpreter Path (the cidx entrypoint is at tmp_path / "cidx").
    """
    interpreter = tmp_path / "cli-venv" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    cidx_bin = tmp_path / "cidx"
    cidx_bin.write_text(f"#!{interpreter}\nfrom code_indexer.cli import cli\ncli()\n")
    return interpreter


def _make_explicit_dispatch(
    calls: list, install_path: str, install_rc: int = 0, install_stderr: str = ""
):
    """subprocess.run side_effect answering every REAL subprocess call issued
    by `_is_user_install()` (the `import code_indexer` probe),
    `_pip_supports_break_system_packages()` (the `pip --version` probe), and
    the actual pip install command -- only the pip install command is
    appended to `calls`. An unrecognized command raises AssertionError
    rather than silently succeeding (established convention from
    test_deployment_executor_hnswlib_cli_sync_1392.py's
    _make_stock_but_importable_dispatch)."""

    def dispatch(cmd: list, **kw: object):
        joined = " ".join(str(c) for c in cmd)
        if "import code_indexer" in joined:
            return Mock(returncode=0, stdout=f"{install_path}\n", stderr="")
        if "--version" in cmd:
            return Mock(
                returncode=0, stdout="pip 23.1 from /path (python 3.9)\n", stderr=""
            )
        if "-e" in cmd:
            calls.append(list(cmd))
            return Mock(returncode=install_rc, stdout="", stderr=install_stderr)
        raise AssertionError(f"Unexpected subprocess command in this test: {cmd}")

    return dispatch


class TestEnsureCliDependenciesSynced:
    """_ensure_cli_dependencies_synced() orchestration: resolve CLI
    interpreter -> no-op if none found -> else run pip install -e . against
    it -> non-fatal WARNING on failure."""

    def test_returns_true_when_no_cli_python_found(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        with (
            patch("shutil.which", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            result = executor._ensure_cli_dependencies_synced()

        assert result is True
        mock_run.assert_not_called()

    def test_pip_install_succeeds_returns_true(
        self, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        interpreter = _setup_cli_entrypoint(tmp_path)
        install_dir = tmp_path / "editable" / "code_indexer"
        install_dir.mkdir(parents=True)
        install_path = str(install_dir / "__init__.py")
        calls: list = []

        executor = _executor(tmp_path)
        with (
            patch("shutil.which", return_value=str(tmp_path / "cidx")),
            patch(
                "subprocess.run",
                side_effect=_make_explicit_dispatch(calls, install_path),
            ),
        ):
            result = executor._ensure_cli_dependencies_synced()

        assert result is True
        assert calls, f"Expected pip install -e . call; captured: {calls}"
        assert str(interpreter) in calls[0], (
            f"Expected the CLI interpreter {interpreter} in the pip install "
            f"command; got: {calls[0]}"
        )

    def test_pip_install_failure_returns_false_and_logs_warning(
        self, tmp_path: Path, patched_data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        interpreter = _setup_cli_entrypoint(tmp_path)
        install_dir = tmp_path / "editable" / "code_indexer"
        install_dir.mkdir(parents=True)
        install_path = str(install_dir / "__init__.py")
        calls: list = []

        executor = _executor(tmp_path)
        with (
            patch("shutil.which", return_value=str(tmp_path / "cidx")),
            patch(
                "subprocess.run",
                side_effect=_make_explicit_dispatch(
                    calls,
                    install_path,
                    install_rc=1,
                    install_stderr="No module named openpyxl",
                ),
            ),
            caplog.at_level("WARNING"),
        ):
            result = executor._ensure_cli_dependencies_synced()

        assert result is False
        warning_text = "\n".join(
            r.message for r in caplog.records if r.levelname == "WARNING"
        )
        assert "DEPLOY-GENERAL-211" in warning_text
        assert str(interpreter) in warning_text

    def test_pip_install_exception_returns_false_and_logs_warning(
        self, tmp_path: Path, patched_data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _setup_cli_entrypoint(tmp_path)

        executor = _executor(tmp_path)
        with (
            patch("shutil.which", return_value=str(tmp_path / "cidx")),
            patch("subprocess.run", side_effect=OSError("boom")),
            caplog.at_level("WARNING"),
        ):
            result = executor._ensure_cli_dependencies_synced()

        assert result is False
        warning_text = "\n".join(
            r.message for r in caplog.records if r.levelname == "WARNING"
        )
        assert "DEPLOY-GENERAL-211" in warning_text


class TestSharedPipInstallHelperReuse:
    """_ensure_cli_dependencies_synced() must reuse pip_install()'s own
    sudo / --break-system-packages decision logic rather than reimplementing
    it. Proven WITHOUT mocking any executor method: the actual pip install
    command dispatched during _ensure_cli_dependencies_synced() must be
    byte-identical to a direct call to the shared _build_pip_install_cmd()
    helper for the same interpreter -- if the method under test built its
    own separate command instead of calling the shared helper, this
    comparison would fail."""

    def test_system_install_command_matches_shared_helper_output(
        self, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        interpreter = _setup_cli_entrypoint(tmp_path)
        # Non-writable, nonexistent parent directory -> _is_user_install()
        # returns False (system install) -> sudo branch.
        install_path = "/usr/lib/python3.9/site-packages/code_indexer/__init__.py"
        calls: list = []

        executor = _executor(tmp_path)
        with (
            patch("shutil.which", return_value=str(tmp_path / "cidx")),
            patch(
                "subprocess.run",
                side_effect=_make_explicit_dispatch(calls, install_path),
            ),
        ):
            result = executor._ensure_cli_dependencies_synced()
            expected_cmd = executor._build_pip_install_cmd(
                str(interpreter), executor._deploy_tmpdir()
            )

        assert result is True
        assert calls == [expected_cmd], (
            "The pip install command actually dispatched must be byte-"
            "identical to _build_pip_install_cmd()'s own output for the same "
            f"interpreter.\nactual: {calls}\nexpected: {expected_cmd}"
        )
        assert expected_cmd[0] == "sudo", "System install must use sudo"

    def test_user_install_command_matches_shared_helper_output(
        self, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        interpreter = _setup_cli_entrypoint(tmp_path)
        install_dir = tmp_path / "editable" / "code_indexer"
        install_dir.mkdir(parents=True)
        install_path = str(install_dir / "__init__.py")
        calls: list = []

        executor = _executor(tmp_path)
        with (
            patch("shutil.which", return_value=str(tmp_path / "cidx")),
            patch(
                "subprocess.run",
                side_effect=_make_explicit_dispatch(calls, install_path),
            ),
        ):
            result = executor._ensure_cli_dependencies_synced()
            expected_cmd = executor._build_pip_install_cmd(
                str(interpreter), executor._deploy_tmpdir()
            )

        assert result is True
        assert calls == [expected_cmd], (
            "The pip install command actually dispatched must be byte-"
            "identical to _build_pip_install_cmd()'s own output for the same "
            f"interpreter.\nactual: {calls}\nexpected: {expected_cmd}"
        )
        assert expected_cmd[0] != "sudo", "User install must NOT use sudo"


class TestBuildPipInstallCmdClusterExtras:
    """Bug #1450: `_build_pip_install_cmd()` must request the `cluster`
    extras group (`.[cluster]`) instead of a bare `.`, in BOTH the sudo
    (system-install) and non-sudo (user-install) branches.

    `psycopg[binary]`/`psycopg-pool` are declared under
    `[project.optional-dependencies] cluster` in pyproject.toml -- NOT the
    base `dependencies` list -- so a plain `pip install -e .` never pulls
    them in. `server/storage/postgres/connection_pool.py` does an
    unconditional module-level `import psycopg` (a deliberate, documented
    invariant, regardless of storage_mode), and `cli.py`'s
    `_install_embedding_stats_writer_for_index()` transitively imports it
    during `cidx index` execution. Because this single shared helper backs
    BOTH `pip_install()` (server's pipx venv) and
    `_ensure_cli_dependencies_synced()` (CLI's system-Python interpreter,
    Bug #1442's self-heal), the CLI's system-Python interpreter never got
    psycopg -- confirmed live in production (recurring
    `cidx-meta-global`/`k8s-wildfly-sandboxes-*-global` refresh failures)
    and reproduced on a solo staging VM via the real automated deploy
    mechanism: `ModuleNotFoundError: No module named 'psycopg'`, wrapped as
    `RuntimeError: semantic indexing on source failed for ...`.
    """

    def test_sudo_branch_uses_cluster_extras(
        self, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        interpreter = _setup_cli_entrypoint(tmp_path)
        # A nonexistent, non-"/.local/" parent directory under tmp_path is
        # both unwritable (os.access returns False for a missing path) and
        # never created by this test -> _is_user_install() returns False
        # (system install) -> sudo branch, without hardcoding any real
        # environment-specific system path.
        install_path = str(
            tmp_path
            / "no-such-system-root"
            / "site-packages"
            / "code_indexer"
            / "__init__.py"
        )
        calls: list = []

        executor = _executor(tmp_path)
        with (
            patch("shutil.which", return_value=str(tmp_path / "cidx")),
            patch(
                "subprocess.run",
                side_effect=_make_explicit_dispatch(calls, install_path),
            ),
        ):
            cmd = executor._build_pip_install_cmd(
                str(interpreter), executor._deploy_tmpdir()
            )

        assert cmd[0] == "sudo", "System install must use sudo"
        assert cmd[-2:] == ["-e", ".[cluster]"], (
            f"Expected the trailing tokens to be -e .[cluster] (Bug #1450); got: {cmd}"
        )

    def test_user_install_branch_uses_cluster_extras(
        self, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        interpreter = _setup_cli_entrypoint(tmp_path)
        install_dir = tmp_path / "editable" / "code_indexer"
        install_dir.mkdir(parents=True)
        install_path = str(install_dir / "__init__.py")
        calls: list = []

        executor = _executor(tmp_path)
        with (
            patch("shutil.which", return_value=str(tmp_path / "cidx")),
            patch(
                "subprocess.run",
                side_effect=_make_explicit_dispatch(calls, install_path),
            ),
        ):
            cmd = executor._build_pip_install_cmd(
                str(interpreter), executor._deploy_tmpdir()
            )

        assert cmd[0] != "sudo", "User install must NOT use sudo"
        assert cmd[-2:] == ["-e", ".[cluster]"], (
            f"Expected the trailing tokens to be -e .[cluster] (Bug #1450); got: {cmd}"
        )


# ---------------------------------------------------------------------------
# execute() wiring -- mirrors the established pattern from
# test_deployment_executor_hnswlib_cli_sync_1392.py's _NOOP_EXECUTE_STEPS /
# _patched_execute
# ---------------------------------------------------------------------------

_NOOP_EXECUTE_STEPS: dict = {
    "git_pull": True,
    "git_submodule_update": True,
    "_build_hnswlib_with_fallback": True,
    "_ensure_cli_hnswlib_capability": True,
    "_ensure_cli_dependencies_synced": True,
    "pip_install": True,
    "_ensure_launch_config": None,
    "_ensure_cidx_repo_root": True,
    "_ensure_git_safe_directory": True,
    "_ensure_git_safe_directory_wildcard": True,
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
    "_calculate_auto_update_hash": "fixed-hash-1442",
}


def _patched_execute(executor: DeploymentExecutor, overrides: dict) -> ExitStack:
    """Patch every execute() step to a no-op success, except for the methods
    named in `overrides` (method_name -> return_value)."""
    stack = ExitStack()
    merged = dict(_NOOP_EXECUTE_STEPS)
    merged.update(overrides)
    for method_name, value in merged.items():
        stack.enter_context(patch.object(executor, method_name, return_value=value))
    return stack


class TestExecuteWiresCliDependenciesSync:
    """execute() must call _ensure_cli_dependencies_synced() as Step 1.8 --
    immediately after Step 1.7's _ensure_cli_hnswlib_capability() and before
    Step 2's pip_install() -- and treat its failure as NON-FATAL, exactly
    like Step 1.7: a failure here targets a wholly independent Python
    environment (the CLI's, not the server's) and must not block the
    server's own restart/config steps.
    """

    def test_execute_calls_step_and_proceeds_past_failure(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        with _patched_execute(executor, {"_ensure_cli_dependencies_synced": False}):
            result = executor.execute()

            assert result is True, (
                "execute() must proceed to a successful completion even "
                "when the CLI dependency sync failed"
            )
            executor._ensure_cli_dependencies_synced.assert_called_once()  # type: ignore[attr-defined]
            # Prove execute() did not short-circuit: the LAST pre-restart
            # step (Rust toolchain, Step 16) must still run.
            executor._ensure_rust_toolchain.assert_called_once()  # type: ignore[attr-defined]

    def test_execute_calls_step_in_order_after_hnswlib_sync_and_before_pip_install(
        self, tmp_path: Path
    ) -> None:
        executor = _executor(tmp_path)
        call_order: list = []

        def _record(name: str):
            def _fn(*args: object, **kwargs: object) -> bool:
                call_order.append(name)
                return True

            return _fn

        with _patched_execute(executor, {}):
            # Override three specific steps with order-recording side_effects
            # instead of plain return_value, layered on top of the no-op stack.
            with (
                patch.object(
                    executor,
                    "_ensure_cli_hnswlib_capability",
                    side_effect=_record("_ensure_cli_hnswlib_capability"),
                ),
                patch.object(
                    executor,
                    "_ensure_cli_dependencies_synced",
                    side_effect=_record("_ensure_cli_dependencies_synced"),
                ),
                patch.object(
                    executor, "pip_install", side_effect=_record("pip_install")
                ),
            ):
                result = executor.execute()

        assert result is True
        relevant_order = [
            name
            for name in call_order
            if name
            in (
                "_ensure_cli_hnswlib_capability",
                "_ensure_cli_dependencies_synced",
                "pip_install",
            )
        ]
        assert relevant_order == [
            "_ensure_cli_hnswlib_capability",
            "_ensure_cli_dependencies_synced",
            "pip_install",
        ], f"Expected this exact step order; got: {relevant_order}"
