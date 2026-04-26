"""
Unit tests for setup-codex-npm-prefix.sh shell script.

Tests verify (TDD RED phase — script does not exist yet):
- test_npm_missing: script aborts clearly when npm absent
- test_system_prefix_switched_to_user_writable: /usr/local prefix -> ~/.npm-global
- test_user_prefix_preserved: ~/.npm-global prefix -> no re-set (idempotent)
- test_path_export_added_to_bashrc: export line added once, no duplicate
- test_codex_install_invoked: npm install -g @openai/codex is called

Following TDD methodology: Tests written FIRST before implementing (RED phase).
"""

import shutil
import stat
import subprocess
import textwrap
from pathlib import Path
from typing import Dict, List, Optional

import pytest

SCRIPT_PATH = Path(__file__).parents[3] / "scripts" / "setup-codex-npm-prefix.sh"

# Resolve bash interpreter at import time (when PATH is normal) so individual
# tests can scrub PATH at runtime without losing the bash interpreter itself.
# `test_npm_missing` deliberately runs with `PATH=<no-tools>` to verify the
# script exits cleanly when npm is absent; with `subprocess.run(["bash", ...])`
# the runtime PATH lookup fails before the script ever runs. Resolving once
# at module load + invoking via the absolute path side-steps that race.
# Falls back to `/bin/bash` (POSIX-standard location) on the rare host where
# bash is not on PATH at import time.
BASH: str = shutil.which("bash") or "/bin/bash"

_SYSTEM_PREFIX = "/usr/local"
_USER_PREFIX_SUFFIX = ".npm-global"


# ---------------------------------------------------------------------------
# Shim factories
# ---------------------------------------------------------------------------


def _make_npm_shim(shim_dir: Path, prefix: str) -> None:
    """
    Fake `npm` that:
    - `npm config get prefix` -> reads shim_dir/npm_prefix.txt (or returns `prefix`)
    - `npm config set prefix <path>` -> writes new prefix to npm_prefix.txt
    - `npm install -g @openai/codex` -> appends to npm_calls.log, exits 0
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    npm_shim = shim_dir / "npm"
    npm_shim.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            SHIM_DIR="{shim_dir}"
            case "$1 $2" in
              "config get")
                if [ "$3" = "prefix" ]; then
                  if [ -f "$SHIM_DIR/npm_prefix.txt" ]; then
                    cat "$SHIM_DIR/npm_prefix.txt"
                  else
                    echo "{prefix}"
                  fi
                fi
                ;;
              "config set")
                echo "$4" > "$SHIM_DIR/npm_prefix.txt"
                ;;
              "install -g")
                echo "NPM_INSTALL_CALLED: $@" >> "$SHIM_DIR/npm_calls.log"
                ;;
              *)
                ;;
            esac
            exit 0
            """
        )
    )
    npm_shim.chmod(npm_shim.stat().st_mode | stat.S_IEXEC)


def _make_codex_shim(bin_dir: Path) -> None:
    """Fake `codex` binary that prints a recognizable version string."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    codex_bin = bin_dir / "codex"
    codex_bin.write_text("#!/bin/sh\necho 'codex 0.1.0-test'\n")
    codex_bin.chmod(codex_bin.stat().st_mode | stat.S_IEXEC)


def _run_script(
    args: List,
    tmp_home: Path,
    shim_dir: Path,
    extra_env: Optional[Dict] = None,
) -> subprocess.CompletedProcess:
    """Run setup-codex-npm-prefix.sh with a controlled, hermetic environment."""
    env = {
        "HOME": str(tmp_home),
        "PATH": str(shim_dir) + ":/usr/bin:/bin",
        "TERM": "dumb",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, str(SCRIPT_PATH)] + args,
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def home_dir(tmp_path):
    """Isolated HOME directory with a pre-existing .bashrc."""
    h = tmp_path / "home"
    h.mkdir()
    (h / ".bashrc").write_text("# existing content\n")
    return h


@pytest.fixture()
def system_prefix_env(tmp_path, home_dir):
    """
    Environment where npm reports /usr/local (system prefix).
    Codex shim pre-created so the version probe succeeds.
    Returns (shim_dir, home_dir).
    """
    shim_dir = tmp_path / "shims"
    _make_npm_shim(shim_dir, prefix=_SYSTEM_PREFIX)
    _make_codex_shim(home_dir / ".npm-global" / "bin")
    return shim_dir, home_dir


@pytest.fixture()
def user_prefix_env(tmp_path, home_dir):
    """
    Environment where npm already reports ~/.npm-global (user prefix).
    Returns (shim_dir, home_dir).
    """
    shim_dir = tmp_path / "shims"
    user_prefix = str(home_dir / ".npm-global")
    _make_npm_shim(shim_dir, prefix=user_prefix)
    _make_codex_shim(home_dir / ".npm-global" / "bin")
    return shim_dir, home_dir


# ---------------------------------------------------------------------------
# Tests 1-5
# ---------------------------------------------------------------------------


def test_npm_missing(tmp_path):
    """
    When npm is not on PATH, script aborts with non-zero exit and
    prints a clear error mentioning 'npm'.

    PATH must be strictly isolated — no /usr/bin or /bin — so the real
    npm binary is unreachable.  _run_script prepends shim_dir then appends
    /usr/bin:/bin, so we override PATH explicitly via extra_env instead.
    """
    empty_shims = tmp_path / "empty_shims"
    empty_shims.mkdir()
    tmp_home = tmp_path / "home"
    tmp_home.mkdir()

    # Override PATH completely: only bash built-ins available, no npm anywhere.
    result = _run_script(
        [],
        tmp_home,
        empty_shims,
        extra_env={"PATH": str(empty_shims)},
    )

    assert result.returncode != 0, (
        f"Expected non-zero exit when npm absent. Got {result.returncode}. "
        f"stderr: {result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "npm" in combined.lower(), (
        f"Expected 'npm' in error output. Got: {combined!r}"
    )


def test_system_prefix_switched_to_user_writable(system_prefix_env):
    """
    When npm reports /usr/local, script calls `npm config set prefix ~/.npm-global`
    and creates the ~/.npm-global directory.
    """
    shim_dir, home_dir = system_prefix_env

    result = _run_script([], home_dir, shim_dir)

    assert result.returncode == 0, (
        f"Expected exit 0. stderr: {result.stderr!r}\nstdout: {result.stdout!r}"
    )
    prefix_file = shim_dir / "npm_prefix.txt"
    assert prefix_file.exists(), "Expected npm config set prefix to have been called"
    new_prefix = prefix_file.read_text().strip()
    assert _USER_PREFIX_SUFFIX in new_prefix, (
        f"Expected new prefix to contain '{_USER_PREFIX_SUFFIX}'. Got: {new_prefix!r}"
    )
    assert (home_dir / ".npm-global").is_dir(), (
        "Expected ~/.npm-global directory to be created"
    )


def test_user_prefix_preserved(user_prefix_env):
    """
    When npm already reports a user-writable prefix, script does NOT
    call `npm config set prefix` (idempotent).
    """
    shim_dir, home_dir = user_prefix_env

    result = _run_script([], home_dir, shim_dir)

    assert result.returncode == 0, (
        f"Expected exit 0. stderr: {result.stderr!r}\nstdout: {result.stdout!r}"
    )
    assert not (shim_dir / "npm_prefix.txt").exists(), (
        "Expected npm config set prefix NOT to be called when prefix already user-writable"
    )


def test_path_export_added_to_bashrc(system_prefix_env, home_dir):
    """
    First run adds the export PATH line to .bashrc exactly once.
    Second run does not add a duplicate.
    """
    shim_dir, _ = system_prefix_env

    # First run
    result1 = _run_script([], home_dir, shim_dir)
    assert result1.returncode == 0, (
        f"First run failed. stderr: {result1.stderr!r}"
    )
    bashrc = home_dir / ".bashrc"
    content_after_first = bashrc.read_text()
    assert ".npm-global" in content_after_first, (
        f"Expected .npm-global export in .bashrc after first run:\n{content_after_first}"
    )

    # Simulate "already set" state: write user prefix to npm_prefix.txt
    (shim_dir / "npm_prefix.txt").write_text(str(home_dir / ".npm-global"))

    # Second run
    result2 = _run_script([], home_dir, shim_dir)
    assert result2.returncode == 0, (
        f"Second run failed. stderr: {result2.stderr!r}"
    )
    content_after_second = bashrc.read_text()
    npm_export_count = content_after_second.count(".npm-global/bin")
    assert npm_export_count == 1, (
        f"Expected exactly 1 .npm-global/bin export, got {npm_export_count}:\n{content_after_second}"
    )


def test_codex_install_invoked(system_prefix_env, home_dir):
    """
    Script calls `npm install -g @openai/codex`.
    """
    shim_dir, _ = system_prefix_env

    result = _run_script([], home_dir, shim_dir)

    assert result.returncode == 0, (
        f"Expected exit 0. stderr: {result.stderr!r}\nstdout: {result.stdout!r}"
    )
    npm_calls_log = shim_dir / "npm_calls.log"
    assert npm_calls_log.exists(), (
        "Expected npm_calls.log — npm install -g was never called"
    )
    assert "@openai/codex" in npm_calls_log.read_text(), (
        "Expected '@openai/codex' in npm calls log"
    )


# ---------------------------------------------------------------------------
# Helpers and fixtures for --update-cidx-server-systemd tests
# ---------------------------------------------------------------------------


def _make_sudo_shim(shim_dir: Path) -> None:
    """
    Fake `sudo` that logs every invocation to shim_dir/sudo_calls.log.
    `sudo tee <file>` reads stdin and writes it to <file>.
    `sudo systemctl ...` only logs.
    """
    sudo_shim = shim_dir / "sudo"
    sudo_shim.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            SHIM_DIR="{shim_dir}"
            echo "SUDO_CALLED: $@" >> "$SHIM_DIR/sudo_calls.log"
            case "$1" in
              tee)
                cat > "$2"
                ;;
            esac
            exit 0
            """
        )
    )
    sudo_shim.chmod(sudo_shim.stat().st_mode | stat.S_IEXEC)


def _make_fake_unit_file(unit_path: Path, npm_bin: str, already_in_path: bool) -> None:
    """Write a minimal cidx-server.service unit file for testing."""
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    path_value = (
        f"{npm_bin}:/usr/local/bin:/usr/bin:/bin"
        if already_in_path
        else "/usr/local/bin:/usr/bin:/bin"
    )
    unit_path.write_text(
        textwrap.dedent(
            f"""\
            [Unit]
            Description=cidx-server

            [Service]
            Environment="PATH={path_value}"
            ExecStart=/usr/bin/python3 -m code_indexer.server

            [Install]
            WantedBy=multi-user.target
            """
        )
    )


@pytest.fixture()
def systemd_env(tmp_path, system_prefix_env, home_dir):
    """
    Consolidated fixture for --update-cidx-server-systemd tests.

    Returns a dict with:
      shim_dir  — Path to the shims directory (npm + sudo shims present)
      home_dir  — isolated HOME
      npm_bin   — resolved ~/.npm-global/bin path string
      unit_file — Path where the fake unit file should be written
      run       — callable(already_in_path, extra_args=[]) -> CompletedProcess
                  Creates the unit file (or skips if already_in_path is None),
                  runs the script with CIDX_SYSTEMD_UNIT_PATH set, and returns
                  the CompletedProcess.
    """
    shim_dir, _ = system_prefix_env
    _make_sudo_shim(shim_dir)
    npm_bin = str(home_dir / ".npm-global" / "bin")
    unit_file = tmp_path / "cidx-server.service"

    def run(already_in_path, include_flag=True):
        if already_in_path is not None:
            _make_fake_unit_file(unit_file, npm_bin, already_in_path)
        flag_args = ["--update-cidx-server-systemd"] if include_flag else []
        return _run_script(
            flag_args,
            home_dir,
            shim_dir,
            extra_env={"CIDX_SYSTEMD_UNIT_PATH": str(unit_file)},
        )

    return {
        "shim_dir": shim_dir,
        "home_dir": home_dir,
        "npm_bin": npm_bin,
        "unit_file": unit_file,
        "run": run,
    }


# ---------------------------------------------------------------------------
# Tests 6-8: noop, prepend, daemon-reload
# ---------------------------------------------------------------------------


def test_systemd_path_already_present_noop(systemd_env):
    """
    When npm bin dir is already in the unit PATH, script prints 'already'
    and does NOT rewrite the file (idempotent — content and mtime unchanged).
    """
    unit_file = systemd_env["unit_file"]
    npm_bin = systemd_env["npm_bin"]
    _make_fake_unit_file(unit_file, npm_bin, already_in_path=True)
    original_content = unit_file.read_text()
    original_mtime = unit_file.stat().st_mtime

    result = systemd_env["run"](already_in_path=None)  # file pre-created above

    assert result.returncode == 0, f"Expected exit 0. stderr: {result.stderr!r}"
    assert "already" in (result.stdout + result.stderr).lower(), (
        f"Expected 'already' in output. Got: {result.stdout + result.stderr!r}"
    )
    assert unit_file.read_text() == original_content, "Unit file was rewritten unexpectedly"
    assert unit_file.stat().st_mtime == original_mtime, "Unit file mtime changed unexpectedly"


def test_systemd_path_prepended_when_missing(systemd_env):
    """
    When npm bin dir is NOT in the unit PATH, the script prepends it so
    the resulting PATH line starts with the npm bin dir.
    """
    npm_bin = systemd_env["npm_bin"]
    unit_file = systemd_env["unit_file"]

    result = systemd_env["run"](already_in_path=False)

    assert result.returncode == 0, f"Expected exit 0. stderr: {result.stderr!r}"
    updated = unit_file.read_text()
    assert npm_bin in updated, f"Expected '{npm_bin}' in updated unit. Got:\n{updated}"
    for line in updated.splitlines():
        if "Environment=" in line and "PATH=" in line:
            path_val = line.split("PATH=", 1)[1].rstrip('"')
            assert path_val.startswith(npm_bin), (
                f"Expected PATH to start with '{npm_bin}'. Got: {path_val!r}"
            )
            break
    else:
        pytest.fail(f"No Environment=PATH line found in updated unit:\n{updated}")


def test_systemd_daemon_reload_invoked_after_write(systemd_env):
    """
    After writing the updated unit file, script calls `sudo systemctl daemon-reload`.
    """
    result = systemd_env["run"](already_in_path=False)

    assert result.returncode == 0, f"Expected exit 0. stderr: {result.stderr!r}"
    sudo_log = systemd_env["shim_dir"] / "sudo_calls.log"
    assert sudo_log.exists(), "Expected sudo_calls.log — sudo was never called"
    assert "daemon-reload" in sudo_log.read_text(), (
        f"Expected 'daemon-reload' in sudo calls. Got:\n{sudo_log.read_text()}"
    )


# ---------------------------------------------------------------------------
# Tests 9-10: missing unit warning; no changes without flag
# ---------------------------------------------------------------------------


def test_systemd_unit_missing_warning_no_crash(systemd_env):
    """
    When the unit file does not exist, script prints WARNING and exits 0
    (npm install portion continues — unit patching is non-fatal).
    """
    # already_in_path=None and unit_file never pre-created => points at nonexistent path
    result = systemd_env["run"](already_in_path=None)

    assert result.returncode == 0, (
        f"Expected exit 0 with missing unit. stderr: {result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "warning" in combined.lower(), (
        f"Expected WARNING in output when unit is missing. Got: {combined!r}"
    )


def test_flag_not_passed_no_systemd_changes(systemd_env):
    """
    Without --update-cidx-server-systemd, the unit file is NEVER read or written
    and sudo daemon-reload is never called.
    """
    unit_file = systemd_env["unit_file"]
    npm_bin = systemd_env["npm_bin"]
    _make_fake_unit_file(unit_file, npm_bin, already_in_path=False)
    original_content = unit_file.read_text()

    result = systemd_env["run"](already_in_path=None, include_flag=False)

    assert result.returncode == 0, f"Expected exit 0. stderr: {result.stderr!r}"
    assert unit_file.read_text() == original_content, (
        "Unit file was modified without --update-cidx-server-systemd flag"
    )
    sudo_log = systemd_env["shim_dir"] / "sudo_calls.log"
    if sudo_log.exists():
        assert "daemon-reload" not in sudo_log.read_text(), (
            "daemon-reload was called without the flag"
        )
