import json
import os
import site
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from tests.utils import real_process_guard as guard
from tests.utils.real_process_guard import RealProcessInUnitTest, classify_spawn

pytest_plugins = ["pytester"]

PY = sys.executable
REPO_ROOT = Path(__file__).resolve().parents[2]


def _shim(bin_dir: Path, name: str, body: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    "argv",
    [
        [PY, "-m", "code_indexer.cli", "--help"],
        [PY, "-m", "code_indexer.cli_fast_entry", "index"],
        [PY, "-m", "code_indexer", "init"],
        ["python3", "-u", "-X", "dev", "-m", "code_indexer.cli", "query"],
        ["timeout", "60", PY, "-m", "code_indexer.cli", "index"],
        [PY.encode(), b"-m", b"code_indexer.cli"],
    ],
)
def test_cli_module_runs_are_cidx(argv: List[object]) -> None:
    assert classify_spawn(argv, None) == "cidx"


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "--version"],
        ["git", "commit", "-m", "code_indexer"],
        ["bash", "-c", "source e2e-automation.sh; wait_for_health"],
        [PY, "-c", "from code_indexer.cli import cli; print(1)"],
        [PY, "-m", "pytest", "tests"],
        [PY, "-mpytest"],
        [PY, "script.py", "-m", "code_indexer.cli"],
        ["sleep", "1"],
        ["cp", "a", "b"],
        ["cargo", "build"],
        [],
    ],
)
def test_other_commands_are_not_classified(argv: List[object]) -> None:
    assert classify_spawn(argv, None) is None


def test_xray_cli_is_its_own_kind() -> None:
    assert (
        classify_spawn(["/x/rust/target/release/xray-cli", "run"], None) == "xray-cli"
    )


def test_cidx_shim_running_code_indexer_is_cidx(tmp_path: Path) -> None:
    _shim(
        tmp_path / "bin", "cidx", f'#!/bin/sh\nexec "{PY}" -m code_indexer.cli "$@"\n'
    )
    env = {"PATH": f"{tmp_path / 'bin'}{os.pathsep}/usr/bin"}
    assert classify_spawn(["cidx", "index"], env) == "cidx"


def test_fake_cidx_shim_without_code_indexer_is_not_classified(tmp_path: Path) -> None:
    _shim(tmp_path / "bin", "cidx", "#!/bin/sh\necho fake\n")
    env = {b"PATH": os.fsencode(f"{tmp_path / 'bin'}{os.pathsep}/usr/bin")}
    assert classify_spawn(["cidx", "index"], env) is None


def test_cidx_shim_is_resolved_on_os_environ_path_without_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shim(tmp_path / "bin", "cidx", "#!/bin/sh\necho fake\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}/usr/bin")
    assert classify_spawn(["cidx", "index"], None) is None


def test_cidx_shim_delegating_to_a_script_that_runs_the_cli_is_cidx(
    tmp_path: Path,
) -> None:
    inner = _shim(
        tmp_path / "bin",
        "inner.py",
        "import runpy\nrunpy.run_module('code_indexer.cli')\n",
    )
    body = f"#!{PY}\nimport os, sys\nos.execvp('unshare', ['unshare', '-rn', '--', {PY!r}, {str(inner)!r}])\n"
    shim = _shim(tmp_path / "bin", "cidx", body)
    assert classify_spawn([str(shim), "index"], None) == "cidx"


def test_cidx_shim_delegating_to_a_pure_fake_is_not_classified(tmp_path: Path) -> None:
    script = _shim(tmp_path / "bin", "fake.py", "import sys\nsys.exit(0)\n")
    shim = _shim(tmp_path / "bin", "cidx", f'#!/bin/sh\nexec "{PY}" "{script}" "$@"\n')
    assert classify_spawn([str(shim), "index"], None) is None


def test_absolute_cidx_path_is_read_directly(tmp_path: Path) -> None:
    shim = _shim(
        tmp_path / "bin", "cidx", "from code_indexer.cli_fast_entry import main\n"
    )
    assert classify_spawn([str(shim), "index"], {"PATH": ""}) == "cidx"


def test_unresolvable_cidx_fails_closed(tmp_path: Path) -> None:
    assert classify_spawn(["cidx", "index"], {"PATH": str(tmp_path)}) == "cidx"


def test_unreadable_cidx_fails_closed(tmp_path: Path) -> None:
    shim = _shim(tmp_path / "bin", "cidx", "#!/bin/sh\necho fake\n")
    shim.chmod(0o111)
    if os.access(shim, os.R_OK):
        pytest.skip("running as a user who can read mode 0111 files")
    assert classify_spawn([str(shim)], None) == "cidx"


@pytest.mark.parametrize(
    "command",
    [
        "cd /x && cidx index",
        "echo hi; python3 -m code_indexer.cli query x",
        "FOO=1 python3 -m code_indexer.cli index | tee log",
        "true || xray-cli run",
    ],
)
def test_shell_strings_are_split_per_segment(command: str, tmp_path: Path) -> None:
    expected = "xray-cli" if "xray-cli" in command else "cidx"
    assert (
        classify_spawn(["/bin/sh", "-c", command], {"PATH": str(tmp_path)}) == expected
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["/bin/sh", "-c", "command cidx --help"],
        ["/bin/sh", "-c", "cd /x && exec cidx index"],
        ["/bin/sh", "-c", "env -i FOO=1 BAR=2 cidx index"],
        ["/bin/sh", "-c", "nohup cidx index &"],
        ["/bin/sh", "-c", "FOO=1 nohup env cidx index"],
        ["env", "FOO=1", "cidx", "index"],
        ["/usr/bin/nohup", "cidx", "index"],
    ],
)
def test_wrapper_commands_before_cidx_are_stripped(
    argv: List[str], tmp_path: Path
) -> None:
    assert classify_spawn(argv, {"PATH": str(tmp_path)}) == "cidx"


@pytest.mark.parametrize(
    "argv",
    [
        ["/bin/sh", "-c", "command -v cidx"],
        ["/bin/sh", "-c", "command -V cidx"],
        ["/bin/sh", "-c", "command -pv cidx && echo found"],
        ["command", "-v", "cidx"],
        ["/bin/sh", "-c", "type cidx"],
        ["/bin/sh", "-c", "which cidx"],
        ["/bin/sh", "-c", "hash cidx"],
    ],
)
def test_command_lookups_are_not_launches(argv: List[str]) -> None:
    assert classify_spawn(argv, {"PATH": "/nonexistent"}) is None


def test_wrapped_cidx_in_a_shell_string_is_caught_by_the_hook(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    _shim(bin_dir, "cidx", f'#!/bin/sh\nexec "{PY}" -m code_indexer.cli "$@"\n')
    env = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin"}
    with pytest.raises(RealProcessInUnitTest):
        subprocess.Popen(
            "command cidx --help",
            shell=True,
            env=env,
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).wait(60)
    assert guard.take_violations(request.node.nodeid)


@pytest.mark.parametrize("command", ["git status && ls", "echo 'unterminated"])
def test_shell_strings_without_cidx_are_not_classified(command: str) -> None:
    assert classify_spawn(["/bin/sh", "-c", command], None) is None


def test_unparseable_shell_string_naming_code_indexer_fails_closed() -> None:
    command = "python3 -m code_indexer.cli 'unterminated"
    assert classify_spawn(["/bin/sh", "-c", command], None) == "cidx"


@pytest.mark.parametrize(
    "event, args, expected",
    [
        ("subprocess.Popen", (PY, [PY, "-m", "x"], None, {"A": "1"}), [PY, "-m", "x"]),
        ("subprocess.Popen", ("git", "git", None, None), ["git"]),
        (
            "os.exec",
            ("/usr/bin/cidx", ["cidx", "index"], {"A": "1"}),
            ["/usr/bin/cidx", "index"],
        ),
        ("os.posix_spawn", ("/bin/git", ["git", "log"], None), ["/bin/git", "log"]),
        ("os.system", ("cidx index",), ["/bin/sh", "-c", "cidx index"]),
    ],
)
def test_spawn_events_are_normalised_to_argv_and_env(
    event: str, args: Tuple[Any, ...], expected: List[str]
) -> None:
    argv, env = guard._spawn_argv_env(event, args)
    assert argv == expected
    assert env == (None if event == "os.system" else args[-1])


def test_unit_conftest_arms_the_guard_in_this_process(
    request: pytest.FixtureRequest,
) -> None:
    import tests.unit.conftest as unit_conftest

    assert unit_conftest.pytest_runtest_protocol is guard.pytest_runtest_protocol
    assert unit_conftest.pytest_runtest_makereport is guard.pytest_runtest_makereport
    with pytest.raises(RealProcessInUnitTest):
        subprocess.Popen([PY, "-m", "code_indexer.cli", "--help"])
    # ^ invariant violation popped here => this test passes; the raise came before fork
    assert guard.take_violations(request.node.nodeid)


CLI_POPEN_ARGS = (PY, [PY, "-m", "code_indexer.cli", "index"], None, None)


@pytest.fixture
def guarded_item(monkeypatch: pytest.MonkeyPatch) -> str:
    nodeid = "tests/unit/test_example.py::test_spawns"
    monkeypatch.setattr(guard, "_active", (nodeid, False))
    monkeypatch.delenv(guard.REPORT_ENV, raising=False)
    return nodeid


def test_audit_records_and_raises_for_an_unmarked_cidx_spawn(guarded_item: str) -> None:
    with pytest.raises(RealProcessInUnitTest) as raised:
        guard._audit("subprocess.Popen", CLI_POPEN_ARGS)
    expected = (
        f"{guarded_item} started a real cidx process ({PY} -m code_indexer.cli index)"
        " without @pytest.mark.slow: mark it slow (slow-automation.sh runs it)"
    )
    assert str(raised.value) == expected
    assert guard.take_violations(guarded_item) == [expected]
    assert guard.take_violations(guarded_item) == []


@pytest.mark.parametrize(
    "active, event, args",
    [
        (("t::marked", True), "subprocess.Popen", CLI_POPEN_ARGS),
        (None, "subprocess.Popen", CLI_POPEN_ARGS),
        (("t::x", False), "subprocess.Popen", ("xray-cli", ["xray-cli"], None, None)),
        (("t::x", False), "subprocess.Popen", ("git", ["git", "log"], None, None)),
        (("t::x", False), "open", ("cidx", "r", 0)),
    ],
)
def test_audit_allows_without_recording(
    monkeypatch: pytest.MonkeyPatch, active: Any, event: str, args: Tuple[Any, ...]
) -> None:
    monkeypatch.setattr(guard, "_active", active)
    monkeypatch.delenv(guard.REPORT_ENV, raising=False)
    guard._audit(event, args)
    assert guard.take_violations("t::x") == []


def test_audit_report_mode_writes_a_row_and_never_raises(
    guarded_item: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report.jsonl"
    monkeypatch.setenv(guard.REPORT_ENV, str(report))
    guard._audit("subprocess.Popen", CLI_POPEN_ARGS)
    guard._audit("subprocess.Popen", ("git", ["git", "log"], None, None))
    row = {"nodeid": guarded_item, "kind": "cidx", "argv0": PY, "allowed": False}
    assert [json.loads(line) for line in report.read_text().splitlines()] == [row]
    assert guard.take_violations(guarded_item) == []


def test_report_write_failure_never_fails_the_spawn(
    guarded_item: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(guard.REPORT_ENV, str(tmp_path / "missing" / "report.jsonl"))
    monkeypatch.setattr(guard, "_report_write_failures", 0, raising=False)
    guard._audit("subprocess.Popen", CLI_POPEN_ARGS)
    guard._audit("subprocess.Popen", CLI_POPEN_ARGS)
    assert guard._report_write_failures == 2
    warnings = [
        line
        for line in capsys.readouterr().err.splitlines()
        if "real-process guard" in line
    ]
    assert len(warnings) == 1
    assert guard.take_violations(guarded_item) == []


class _BrokenStderr:
    def write(self, text: str) -> int:
        raise OSError("stderr closed")


def test_report_warning_write_failure_is_swallowed(
    guarded_item: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(guard.REPORT_ENV, str(tmp_path / "missing" / "report.jsonl"))
    monkeypatch.setattr(guard, "_report_write_failures", 0)
    monkeypatch.setattr(sys, "stderr", _BrokenStderr())
    guard._audit("subprocess.Popen", CLI_POPEN_ARGS)
    assert guard._report_write_failures == 1


def test_popen_executable_argument_is_classified(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    shim = _shim(
        tmp_path / "bin", "cidx", f'#!/bin/sh\nexec "{PY}" -m code_indexer.cli "$@"\n'
    )
    with pytest.raises(RealProcessInUnitTest):
        subprocess.Popen(
            ["unrelated-name", "--help"],
            executable=str(shim),
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).wait(60)
    assert guard.take_violations(request.node.nodeid)


GUARD_CONFTEST = (
    "from tests.utils.real_process_guard import (  # noqa: F401\n"
    "    install, pytest_runtest_makereport, pytest_runtest_protocol,\n"
    ")\n"
    "install()\n"
)
VIOLATION = (
    "*started a real cidx process (*) without @pytest.mark.slow:"
    " mark it slow (slow-automation.sh runs it)*"
)
CLI_HELP = f"subprocess.run([{PY!r}, '-m', 'code_indexer.cli', '--help'], check=True)"


@pytest.mark.slow
class TestGuardInARealSession:
    @pytest.fixture(autouse=True)
    def _session(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ^ external pytester sets HOME = its tmp dir => child loses the user site-packages holding pytest
        user_site = site.getusersitepackages()
        paths = [
            str(REPO_ROOT),
            str(REPO_ROOT / "src"),
            user_site,
            os.environ.get("PYTHONPATH", ""),
        ]
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in paths if p))
        monkeypatch.delenv(guard.REPORT_ENV, raising=False)
        pytester.makeconftest(GUARD_CONFTEST)
        self.pytester = pytester

    def _run(self, body: str) -> pytest.RunResult:
        source = "import os, subprocess, sys, threading\nimport pytest\n\n"
        self.pytester.makepyfile(test_inner=source + body)
        return self.pytester.runpytest_subprocess("-p", "no:cacheprovider", timeout=120)

    def test_unmarked_cli_run_fails_with_the_message(self) -> None:
        result = self._run(f"def test_x():\n    {CLI_HELP}\n")
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(["*test_inner.py::test_x " + VIOLATION])

    def test_slow_marked_cli_run_passes(self) -> None:
        result = self._run(f"@pytest.mark.slow\ndef test_x():\n    {CLI_HELP}\n")
        result.assert_outcomes(passed=1)

    def test_git_passes(self) -> None:
        result = self._run(
            "def test_x():\n    subprocess.run(['git', '--version'], check=True)\n"
        )
        result.assert_outcomes(passed=1)

    def test_os_system_cli_run_fails(self) -> None:
        command = f"{PY} -m code_indexer.cli --help"
        result = self._run(f"def test_x():\n    os.system({command!r})\n")
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines([VIOLATION])

    def test_fake_cidx_shim_passes(self) -> None:
        bin_dir = self.pytester.path / "bin"
        _shim(bin_dir, "cidx", "#!/bin/sh\necho fake\n")
        body = (
            "def test_x():\n"
            f"    env = dict(os.environ, PATH={str(bin_dir)!r} + os.pathsep + os.environ['PATH'])\n"
            "    subprocess.run(['cidx', 'index'], env=env, check=True)\n"
        )
        self._run(body).assert_outcomes(passed=1)

    def test_shim_running_the_real_cli_fails(self) -> None:
        bin_dir = self.pytester.path / "bin"
        _shim(bin_dir, "cidx", f'#!/bin/sh\nexec "{PY}" -m code_indexer.cli "$@"\n')
        body = (
            "def test_x(monkeypatch):\n"
            f"    monkeypatch.setenv('PATH', {str(bin_dir)!r} + os.pathsep + os.environ['PATH'])\n"
            "    subprocess.run(['cidx', '--help'], check=True)\n"
        )
        result = self._run(body)
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines([VIOLATION])

    def test_spawn_from_a_thread_fails(self) -> None:
        body = (
            "def test_x():\n"
            f"    worker = threading.Thread(target=lambda: {CLI_HELP})\n"
            "    worker.start()\n"
            "    worker.join(60)\n"
        )
        result = self._run(body)
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines([VIOLATION])

    def test_swallowed_guard_error_still_fails(self) -> None:
        body = (
            "def test_x():\n"
            "    try:\n"
            f"        {CLI_HELP}\n"
            "    except Exception:\n"
            "        pass\n"
        )
        result = self._run(body)
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines([VIOLATION])

    def test_spawn_in_a_fixture_fails_the_test(self) -> None:
        body = f"@pytest.fixture\ndef spawned():\n    {CLI_HELP}\n\ndef test_x(spawned):\n    pass\n"
        result = self._run(body)
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines([VIOLATION])

    def test_xray_cli_spawn_is_allowed(self) -> None:
        xray = _shim(self.pytester.path / "bin", "xray-cli", "#!/bin/sh\nexit 0\n")
        body = f"def test_x():\n    subprocess.run([{str(xray)!r}], check=True)\n"
        self._run(body).assert_outcomes(passed=1)

    def test_report_mode_records_and_never_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = self.pytester.path / "report.jsonl"
        monkeypatch.setenv(guard.REPORT_ENV, str(report))
        xray = _shim(self.pytester.path / "bin", "xray-cli", "#!/bin/sh\nexit 0\n")
        body = (
            f"def test_cli():\n    {CLI_HELP}\n\n"
            f"@pytest.mark.slow\ndef test_slow_cli():\n    {CLI_HELP}\n\n"
            f"def test_xray():\n    subprocess.run([{str(xray)!r}], check=True)\n\n"
            "def test_git():\n    subprocess.run(['git', '--version'], check=True)\n"
        )
        self._run(body).assert_outcomes(passed=4)
        rows = [json.loads(line) for line in report.read_text().splitlines()]
        assert rows == [
            {
                "nodeid": "test_inner.py::test_cli",
                "kind": "cidx",
                "argv0": PY,
                "allowed": False,
            },
            {
                "nodeid": "test_inner.py::test_slow_cli",
                "kind": "cidx",
                "argv0": PY,
                "allowed": True,
            },
            {
                "nodeid": "test_inner.py::test_xray",
                "kind": "xray-cli",
                "argv0": str(xray),
                "allowed": True,
            },
        ]
