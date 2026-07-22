"""Tests for Bug #1392: sync the custom hnswlib fork into the CLI's separate
system-wide Python environment during deploy.

Production bug: the auto-updater's `_build_hnswlib_with_fallback()` only
ever built hnswlib into the SERVER's own Python env (`_get_server_python()`).
Real `cidx` CLI indexing subprocesses run under a wholly separate,
system-wide Python environment that received no equivalent build step, so
that environment could silently drift to a stock PyPI hnswlib (missing
check_integrity()/repair_orphans()), causing every finalize-time orphan
detect+repair call to fail with AttributeError fleet-wide.

Fix: `_get_cli_python_interpreter()` resolves the CLI's interpreter via the
`cidx` console-script shebang (NOT hardcoded paths); `_hnswlib_has_full_capability()`
probes for check_integrity/repair_orphans specifically (stricter than the
existing `_hnswlib_importable()`, which just probes `import hnswlib`);
`_ensure_cli_hnswlib_capability()` orchestrates: resolve interpreter -> skip
if already capable -> else `_build_hnswlib_with_fallback(python_path=...)` ->
loud actionable ERROR on failure (non-fatal to the overall deploy).

Mocking strategy (established convention from
test_deployment_executor_hnswlib_fallback.py / test_deploy_user_install_1245.py):
- Probe unit tests: patch subprocess.run / shutil.which / pathlib.Path directly.
- Orchestration tests: patch.object() the executor's own helper methods to
  isolate _ensure_cli_hnswlib_capability's control flow from their internals.
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
    """Patch _cidx_data_dir to tmp_path/.cidx-server (established convention
    from test_deploy_user_install_1245.py) so filesystem ops (e.g. the
    hnswlib last-built-commit marker) stay confined to tmp_path."""
    data_dir = tmp_path / ".cidx-server"
    with patch.object(_de_mod, "_cidx_data_dir", data_dir):
        yield data_dir


def _make_capturing_dispatch(calls: list, pip_version: str = "23.1"):
    """Return a subprocess.run side_effect that captures all calls and
    handles pip --version (established pattern from
    test_deploy_user_install_1245.py)."""

    def dispatch(cmd: list, **kw: object):
        from unittest.mock import Mock

        calls.append(list(cmd))
        if "-m" in cmd and "pip" in cmd and "--version" in cmd:
            return Mock(
                returncode=0,
                stdout=f"pip {pip_version} from /path (python 3.9)\n",
                stderr="",
            )
        return Mock(returncode=0, stdout="", stderr="")

    return dispatch


def _make_hnswlib_path(tmp_path: Path) -> Path:
    """Create a minimal third_party/hnswlib directory with setup.py."""
    hnswlib_path = tmp_path / "third_party" / "hnswlib"
    hnswlib_path.mkdir(parents=True)
    (hnswlib_path / "setup.py").write_text("# setup")
    return hnswlib_path


class TestGetCliPythonInterpreter:
    """Unit tests for _get_cli_python_interpreter()."""

    def test_returns_none_when_cidx_not_on_path(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        with patch("shutil.which", return_value=None) as mock_which:
            result = executor._get_cli_python_interpreter()
        mock_which.assert_called_once_with("cidx")
        assert result is None

    def test_found_via_plain_shebang(self, tmp_path: Path) -> None:
        interpreter = tmp_path / "python3"
        interpreter.write_text("#!/bin/sh\n")
        cidx_bin = tmp_path / "cidx"
        cidx_bin.write_text(
            f"#!{interpreter}\nfrom code_indexer.cli import cli\ncli()\n"
        )

        executor = _executor(tmp_path)
        with patch("shutil.which", return_value=str(cidx_bin)):
            result = executor._get_cli_python_interpreter()
        assert result == str(interpreter)

    def test_found_via_env_wrapped_shebang(self, tmp_path: Path) -> None:
        interpreter = tmp_path / "python3"
        interpreter.write_text("#!/bin/sh\n")
        cidx_bin = tmp_path / "cidx"
        cidx_bin.write_text(
            "#!/usr/bin/env python3\nfrom code_indexer.cli import cli\ncli()\n"
        )

        executor = _executor(tmp_path)

        def _which(name: str):
            if name == "cidx":
                return str(cidx_bin)
            if name == "python3":
                return str(interpreter)
            return None

        with patch("shutil.which", side_effect=_which):
            result = executor._get_cli_python_interpreter()
        assert result == str(interpreter)

    def test_returns_none_when_entrypoint_unreadable(self, tmp_path: Path) -> None:
        cidx_bin = tmp_path / "cidx"
        # Never created -- Path.read_text() raises FileNotFoundError.
        executor = _executor(tmp_path)
        with patch("shutil.which", return_value=str(cidx_bin)):
            result = executor._get_cli_python_interpreter()
        assert result is None

    def test_returns_none_when_resolved_interpreter_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        missing_interpreter = tmp_path / "does-not-exist-python3"
        cidx_bin = tmp_path / "cidx"
        cidx_bin.write_text(f"#!{missing_interpreter}\ncli()\n")

        executor = _executor(tmp_path)
        with patch("shutil.which", return_value=str(cidx_bin)):
            result = executor._get_cli_python_interpreter()
        assert result is None


class TestHnswlibHasFullCapability:
    """Unit tests for _hnswlib_has_full_capability()."""

    def test_true_when_probe_exits_zero(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = executor._hnswlib_has_full_capability("/usr/bin/python3")
        assert result is True

    def test_false_when_probe_exits_nonzero(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            result = executor._hnswlib_has_full_capability("/usr/bin/python3")
        assert result is False

    def test_false_on_subprocess_exception(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        with patch("subprocess.run", side_effect=OSError("boom")):
            result = executor._hnswlib_has_full_capability("/usr/bin/python3")
        assert result is False


class TestBuildCustomHnswlibPythonPathParam:
    """build_custom_hnswlib() accepts an optional python_path override.

    subprocess.run is the only mocked boundary here (the established
    external-dependency mocking convention for this file) -- no executor
    methods are patched. In a tmp_path sandbox (not a git repo, no
    last-built-commit marker on disk thanks to patched_data_dir), the real
    _get_hnswlib_submodule_commit()/_get_last_built_hnswlib_commit() both
    naturally return None, so the pre-existing "skip rebuild" optimization
    does not trigger and the full build path genuinely executes.
    """

    def test_uses_provided_python_path_not_server_python(
        self, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        executor = _executor(tmp_path)
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []
        cli_python = str(tmp_path / "cli-venv" / "bin" / "python3")

        with patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)):
            executor.build_custom_hnswlib(
                hnswlib_path=hnswlib_path, python_path=cli_python
            )

        hnswlib_calls = [c for c in calls if "--force-reinstall" in c]
        assert hnswlib_calls, f"Expected hnswlib install call; all calls: {calls}"
        assert cli_python in hnswlib_calls[0], (
            f"Expected provided python_path {cli_python} in command; got: "
            f"{hnswlib_calls[0]}"
        )
        # None of the captured commands should reference the real
        # _get_server_python() ExecStart-parsing path (which shells out to
        # `sudo cat /etc/systemd/system/...service`).
        assert not any("systemd" in str(part) for cmd in calls for part in cmd)


class TestBuildHnswlibWithFallbackPythonPathParam:
    """_build_hnswlib_with_fallback() threads python_path to its
    build_custom_hnswlib() collaborator call(s). Mocking build_custom_hnswlib
    isolates _build_hnswlib_with_fallback's OWN branching/orchestration logic
    (submodule-exists -> which call site, with which args) from the
    collaborator's internals -- the standard immediate-collaborator
    interaction-testing pattern, distinct from stubbing out unrelated
    internal helpers of the method under test itself."""

    def test_threads_python_path_to_submodule_build(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        _make_hnswlib_path(tmp_path)  # submodule setup.py present
        cli_python = str(tmp_path / "cli-venv" / "bin" / "python3")

        with patch.object(
            executor, "build_custom_hnswlib", return_value=True
        ) as mock_build:
            result = executor._build_hnswlib_with_fallback(python_path=cli_python)

        assert result is True
        mock_build.assert_called_once_with(hnswlib_path=None, python_path=cli_python)

    def test_threads_python_path_to_fallback_build(self, tmp_path: Path) -> None:
        from code_indexer.server.auto_update.deployment_executor import (
            HNSWLIB_FALLBACK_PATH,
        )

        executor = _executor(tmp_path)
        # No submodule setup.py at tmp_path/third_party/hnswlib -> fallback branch.
        cli_python = str(tmp_path / "cli-venv" / "bin" / "python3")

        with (
            patch.object(executor, "_clone_hnswlib_standalone", return_value=True),
            patch.object(
                executor, "build_custom_hnswlib", return_value=True
            ) as mock_build,
        ):
            result = executor._build_hnswlib_with_fallback(python_path=cli_python)

        assert result is True
        mock_build.assert_called_once_with(
            hnswlib_path=HNSWLIB_FALLBACK_PATH, python_path=cli_python
        )


class TestEnsureCliHnswlibCapability:
    """_ensure_cli_hnswlib_capability() orchestration: resolve CLI interpreter
    -> skip if already capable -> else build -> loud actionable error on
    failure (non-fatal). Only true external boundaries (shutil.which,
    subprocess.run) are mocked -- the real _get_cli_python_interpreter() and
    _build_hnswlib_with_fallback() collaborators run for real."""

    def test_returns_true_when_no_cli_python_found(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        with (
            patch("shutil.which", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            result = executor._ensure_cli_hnswlib_capability()

        assert result is True
        mock_run.assert_not_called()

    def test_skips_rebuild_when_already_capable(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        cli_python = str(tmp_path / "cli-venv" / "bin" / "python3")
        with (
            patch.object(
                executor, "_get_cli_python_interpreter", return_value=cli_python
            ),
            patch.object(executor, "_hnswlib_has_full_capability", return_value=True),
            patch.object(executor, "_build_hnswlib_with_fallback") as mock_build,
        ):
            result = executor._ensure_cli_hnswlib_capability()

        assert result is True
        mock_build.assert_not_called()

    def test_runs_build_and_returns_true_on_success(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        cli_python = str(tmp_path / "cli-venv" / "bin" / "python3")
        with (
            patch.object(
                executor, "_get_cli_python_interpreter", return_value=cli_python
            ),
            patch.object(executor, "_hnswlib_has_full_capability", return_value=False),
            patch.object(
                executor, "_build_hnswlib_with_fallback", return_value=True
            ) as mock_build,
        ):
            result = executor._ensure_cli_hnswlib_capability()

        assert result is True
        mock_build.assert_called_once_with(python_path=cli_python)

    def test_returns_false_and_logs_actionable_error_on_build_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # cli_python does not exist on disk -> the real (unmocked)
        # _get_python_site_packages()/_get_hnswlib_submodule_commit() probes
        # naturally fail closed to None, exercising their real behavior.
        executor = _executor(tmp_path)
        cli_python = str(tmp_path / "cli-venv" / "bin" / "python3")
        with (
            patch.object(
                executor, "_get_cli_python_interpreter", return_value=cli_python
            ),
            patch.object(executor, "_hnswlib_has_full_capability", return_value=False),
            patch.object(executor, "_build_hnswlib_with_fallback", return_value=False),
            caplog.at_level("ERROR"),
        ):
            result = executor._ensure_cli_hnswlib_capability()

        assert result is False
        error_text = "\n".join(
            r.message for r in caplog.records if r.levelname == "ERROR"
        )
        assert cli_python in error_text
        assert "docs/hnswlib-custom-build.md" in error_text


# ---------------------------------------------------------------------------
# execute() wiring -- mirrors the established pattern from
# test_deploy_optional_steps_nonfatal_1254.py::_NOOP_EXECUTE_STEPS /
# _patched_execute
# ---------------------------------------------------------------------------

_NOOP_EXECUTE_STEPS: dict = {
    "git_pull": True,
    "git_submodule_update": True,
    "_build_hnswlib_with_fallback": True,
    "_ensure_cli_hnswlib_capability": True,
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
    "_calculate_auto_update_hash": "fixed-hash-1392",
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


class TestExecuteWiresCliHnswlibSync:
    """execute() must call _ensure_cli_hnswlib_capability() and treat its
    failure as NON-FATAL (unlike Step 1.6's server-python build, which
    hard-aborts) -- a failure here targets a wholly independent Python
    environment and must not block the server's own restart/config steps.
    """

    def test_execute_calls_step_and_proceeds_past_failure(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        with _patched_execute(executor, {"_ensure_cli_hnswlib_capability": False}):
            result = executor.execute()

            assert result is True, (
                "execute() must proceed to a successful completion even "
                "when the CLI hnswlib capability sync failed"
            )
            executor._ensure_cli_hnswlib_capability.assert_called_once()  # type: ignore[attr-defined]
            # Prove execute() did not short-circuit: the LAST pre-restart
            # step (Rust toolchain, Step 16) must still run.
            executor._ensure_rust_toolchain.assert_called_once()  # type: ignore[attr-defined]


class TestGetPythonSitePackages:
    """Unit tests for _get_python_site_packages() (diagnostic-only helper)."""

    def test_returns_stripped_stdout_on_success(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "/usr/lib/python3.9/site-packages\n"
            result = executor._get_python_site_packages("/usr/bin/python3")
        assert result == "/usr/lib/python3.9/site-packages"


def _make_stock_but_importable_dispatch(
    calls: list, submodule_commit=None, force_reinstall_rc: int = 0
):
    """subprocess.run side_effect simulating a target interpreter whose
    hnswlib is STOCK -- the plain ``import hnswlib`` probe
    (_hnswlib_importable) succeeds, but the stricter
    ``check_integrity``/``repair_orphans`` probe (_hnswlib_has_full_capability)
    fails -- the exact drift #1392 exists to detect. Also answers every REAL
    subprocess call issued by _ensure_build_dependencies() / _is_user_install()
    / _get_hnswlib_submodule_commit() / the pybind11 + hnswlib pip install
    commands, so those collaborator methods can run UNMOCKED (only
    subprocess.run itself is a mocked external boundary, per this file's own
    established Mocking strategy) inside a tmp_path sandbox that is neither a
    real git repo nor a real build host. Every branch is explicit -- an
    unrecognized command raises AssertionError rather than silently
    succeeding, so an unmodeled subprocess call fails the test loudly instead
    of masking a gap in test coverage.

    ``submodule_commit``: if given, `git ls-files -s third_party/hnswlib`
    reports it as the current submodule commit; if None, the git call fails
    (as it naturally would in a non-git tmp_path), so `_get_hnswlib_submodule_
    commit()` returns None.

    ``force_reinstall_rc``: return code for the actual hnswlib
    `--force-reinstall` pip install call (0 = build succeeds, non-zero =
    genuine build failure).
    """

    def dispatch(cmd: list, **kw: object):
        calls.append(list(cmd))
        joined = " ".join(cmd)
        if cmd[:3] == ["git", "ls-files", "-s"]:
            if submodule_commit is None:
                return Mock(returncode=1, stdout="", stderr="")
            return Mock(
                returncode=0,
                stdout=f"100644 {submodule_commit} 0\tthird_party/hnswlib\n",
                stderr="",
            )
        if cmd == ["which", "g++"]:
            return Mock(returncode=0, stdout="/usr/bin/g++\n", stderr="")
        if "check_integrity" in joined:
            # _hnswlib_has_full_capability probe: stock hnswlib lacks it.
            return Mock(returncode=1, stdout="", stderr="")
        if "import hnswlib" in joined:
            # _hnswlib_importable probe: stock hnswlib IS importable.
            return Mock(returncode=0, stdout="", stderr="")
        if "import code_indexer" in joined:
            # _is_user_install probe -- empty stdout conservatively
            # classifies as system install; irrelevant to what these tests
            # assert (whether the fork build actually runs / its real
            # success-vs-failure result).
            return Mock(returncode=0, stdout="", stderr="")
        if "-m" in cmd and "pip" in cmd and "--version" in cmd:
            return Mock(
                returncode=0,
                stdout="pip 23.1 from /path (python 3.9)\n",
                stderr="",
            )
        if "pybind11" in cmd:
            return Mock(returncode=0, stdout="", stderr="")
        if "--force-reinstall" in cmd:
            return Mock(
                returncode=force_reinstall_rc,
                stdout="",
                stderr=(
                    "" if force_reinstall_rc == 0 else "Build failed: compiler error"
                ),
            )
        raise AssertionError(f"Unexpected subprocess command in this test: {cmd}")

    return dispatch


class TestSkipGuardCapabilityAwareRemediation:
    """Code-review remediation (CRITICAL finding): the deploy pipeline's
    standard server-builds-first ordering (Step 1.6 server build -> Step 1.7
    CLI sync) let the CLI hnswlib sync step silently no-op.

    Root cause: build_custom_hnswlib()'s skip-rebuild guard and its
    demote-failure-to-WARNING check both gated on `_hnswlib_importable()`
    (plain `import hnswlib` probe) instead of `_hnswlib_has_full_capability()`
    (probes for check_integrity/repair_orphans specifically). Combined with
    the GLOBAL (not per-interpreter) last-built-commit marker file, a prior
    server-python build made the CLI-python build silently skip (or a
    genuine CLI-python build failure silently succeed) whenever the CLI's
    stock PyPI hnswlib happened to still be importable.

    Only subprocess.run and shutil.which (true external process boundaries)
    are mocked below -- every DeploymentExecutor collaborator method
    (_get_cli_python_interpreter, _get_hnswlib_submodule_commit,
    _get_last_built_hnswlib_commit, _ensure_build_dependencies,
    _is_user_install, _hnswlib_has_full_capability, _hnswlib_importable) runs
    for real against the faked subprocess responses, per this file's own
    established Mocking strategy (see module docstring).
    """

    def test_cli_sync_builds_for_real_even_though_global_marker_already_set_by_server_build(
        self, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """Reproduces the EXACT staging failure sequence: Step 1.6 (server
        build) already wrote the GLOBAL last-built-commit marker to the
        current fork commit. Step 1.7 then calls
        _ensure_cli_hnswlib_capability() -> _build_hnswlib_with_fallback() ->
        build_custom_hnswlib(python_path=cli_python) targeting the CLI's
        SEPARATE interpreter, whose hnswlib is merely stock-but-importable.
        The fork build must ACTUALLY RUN for the CLI's interpreter -- it must
        NOT be falsely skipped just because the marker (written by a
        DIFFERENT interpreter's build) matches the current commit.
        """
        executor = _executor(tmp_path)
        _make_hnswlib_path(tmp_path)
        current_fork_commit = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

        # Real CLI interpreter + cidx entrypoint (established convention from
        # TestGetCliPythonInterpreter.test_found_via_plain_shebang) -- only
        # shutil.which (a true external boundary) is mocked; the REAL
        # _get_cli_python_interpreter() shebang-resolution logic executes.
        cli_python = tmp_path / "cli-venv" / "bin" / "python3"
        cli_python.parent.mkdir(parents=True)
        cli_python.write_text("#!/bin/sh\n")
        cidx_bin = tmp_path / "cidx"
        cidx_bin.write_text(
            f"#!{cli_python}\nfrom code_indexer.cli import cli\ncli()\n"
        )

        # Step 1.6 already happened: the SERVER's build wrote the GLOBAL
        # marker to the current fork commit.
        executor._save_last_built_hnswlib_commit(current_fork_commit)

        calls: list = []
        dispatch = _make_stock_but_importable_dispatch(
            calls, submodule_commit=current_fork_commit
        )

        with (
            patch("shutil.which", return_value=str(cidx_bin)),
            patch("subprocess.run", side_effect=dispatch),
        ):
            result = executor._ensure_cli_hnswlib_capability()

        assert result is True
        pip_install_calls = [c for c in calls if "--force-reinstall" in c]
        assert pip_install_calls, (
            "The CLI hnswlib fork build must ACTUALLY RUN even though the "
            "GLOBAL last-built-commit marker already matches the current "
            "commit -- that marker was written by the SERVER's build, not "
            "the CLI's, and stock-but-importable hnswlib on the CLI's "
            "interpreter must not be mistaken for the fork. "
            f"All captured subprocess calls: {calls}"
        )
        assert any(str(cli_python) in c for c in pip_install_calls), (
            f"Expected the fork build to target the CLI's interpreter "
            f"({cli_python}); got: {pip_install_calls}"
        )

    def test_demote_to_warning_check_is_capability_aware_not_importable_only(
        self, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """A genuine build failure targeting an interpreter whose hnswlib is
        merely stock (importable but lacking check_integrity/repair_orphans)
        must be a REAL failure (return False) -- not demoted to a false
        success just because stock hnswlib is still importable.
        """
        executor = _executor(tmp_path)
        hnswlib_path = _make_hnswlib_path(tmp_path)
        cli_python = str(tmp_path / "cli-venv" / "bin" / "python3")

        calls: list = []
        dispatch = _make_stock_but_importable_dispatch(calls, force_reinstall_rc=1)

        with patch("subprocess.run", side_effect=dispatch):
            result = executor.build_custom_hnswlib(
                hnswlib_path=hnswlib_path, python_path=cli_python
            )

        assert result is False, (
            "A build failure targeting a stock-but-importable interpreter "
            "must be a REAL failure, not falsely demoted to success just "
            "because stock hnswlib is still importable."
        )
