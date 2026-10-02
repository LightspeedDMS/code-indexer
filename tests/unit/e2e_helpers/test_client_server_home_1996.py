"""Bug #1996: every e2e phase's pytest process (and the CLI subprocesses it
spawns) gets its OWN fresh server home -- never the developer's real
~/.cidx-server -- and the harness refuses to delete anything that is, holds
or sits inside that real home.

The script is source-safe, so the real helpers run in a bash subprocess;
only ``python3`` is replaced by a shell function that reports the
environment it was handed (the expensive pytest run is the one thing not
executed).  HOME is a FAKE home holding a fake ``.cidx-server`` with a
sentinel file, so even a broken refusal can never touch the real one.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
E2E_SCRIPT = REPO_ROOT / "e2e-automation.sh"
BASH_TIMEOUT_SECONDS = 60
# ${VAR:-} keeps the stub safe under the sourced script's `set -u`.
_REPORT_ENV = (
    'python3() { echo "SERVER_DIR=${CIDX_SERVER_DATA_DIR:-}"; '
    'echo "DATA_DIR=${CIDX_DATA_DIR:-}"; echo "UNIT_DIR=${SYSTEMD_UNIT_DIR:-}"; }'
)


_PHASE7_HELPERS = Path("tests/e2e/siem_delivery/phase7_helpers.sh")
_OTEL_SUBCHECK = Path("tests/e2e/server/test_21_otel_live_collector_1676.py")
_PHASE_TEST_DIR = Path("tests/e2e/cli_standalone")
# Neutral placeholders the phase helpers reference under `set -u`.
_BASE_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "E2E_ADMIN_USER": "example-admin",
    "E2E_ADMIN_PASS": "example-password",
    "E2E_VOYAGE_API_KEY": "",
}
# Ports: the harness default for run_phase servers, plus one per live server.
DEFAULT_PHASE_SERVER_PORT = 8899
STUB_ROUND_TRIP_PORT = 8123
PHASE4_PORT, FAULT_PORT, PG_PORT, SIEM_PORT = 18899, 18900, 18901, 18904


def _stub_flags(port: int) -> dict:
    """What a stub unit for a server on *port* must yield."""
    return {"host": "127.0.0.1", "port": port, "workers": 1}


def _parsed_stub(unit_dir: Path) -> dict:
    """What the REAL live-ExecStart reader parses from *unit_dir*."""
    code = (
        "import json\n"
        "from code_indexer.server.auto_update.deployment_executor import "
        "read_execstart_flags\n"
        "print(json.dumps(read_execstart_flags()))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=BASH_TIMEOUT_SECONDS,
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "SYSTEMD_UNIT_DIR": str(unit_dir),
        },
    )
    assert result.returncode == 0, result.stderr
    parsed: dict = json.loads(result.stdout.strip().splitlines()[-1])
    return parsed


def _harness_root(home: Path) -> Path:
    """Where the hermetic harness copy for *home* lives."""
    return home.parent / "harness"


def _harness_copy(home: Path) -> None:
    """The harness in a temp tree WITHOUT the developer's (gitignored)
    .e2e-automation, which would override the E2E_* values a test passes."""
    root = _harness_root(home)
    (root / _PHASE7_HELPERS.parent).mkdir(parents=True)
    shutil.copy2(E2E_SCRIPT, root / E2E_SCRIPT.name)
    shutil.copy2(REPO_ROOT / _PHASE7_HELPERS, root / _PHASE7_HELPERS)
    (root / _PHASE_TEST_DIR).mkdir(parents=True)
    (root / _OTEL_SUBCHECK).parent.mkdir(parents=True)
    (root / _OTEL_SUBCHECK).write_text("")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A fake HOME whose .cidx-server holds a sentinel that must survive,
    next to a hermetic copy of the harness (see _harness_copy)."""
    fake = tmp_path / "home"
    (fake / ".cidx-server").mkdir(parents=True)
    (fake / ".cidx-server" / "sentinel").write_text("keep")
    (fake / ".tmp").mkdir()
    _harness_copy(fake)
    return fake


def _bash(home: Path, snippet: str, **env: str) -> subprocess.CompletedProcess[str]:
    root = _harness_root(home)
    script = f'source "{root / E2E_SCRIPT.name}"\n{_REPORT_ENV}\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=BASH_TIMEOUT_SECONDS,
        env={**_BASE_ENV, "HOME": str(home), **env},
    )


def _sentinel_intact(home: Path) -> bool:
    return (home / ".cidx-server" / "sentinel").read_text() == "keep"


def test_default_client_server_home_is_inside_the_scratch_root(home: Path) -> None:
    result = _bash(home, 'echo "HOME_DIR=$E2E_CLIENT_SERVER_HOME"')
    assert result.returncode == 0, result.stderr
    assert f"HOME_DIR={home}/.tmp/cidx-e2e-client-server-home" in result.stdout


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "{home}",
        "{home}/",
        "{home}/.cidx-server",
        "{home}/.cidx-server/data",
        "{home}/.tmp/../.cidx-server",
    ],
)
def test_safe_to_wipe_refuses_real_home_conflicts(home: Path, path: str) -> None:
    result = _bash(home, f'safe_to_wipe LABEL "{path.format(home=home)}"')

    assert result.returncode != 0
    assert "REFUSING to wipe LABEL" in result.stderr, result.stderr
    assert _sentinel_intact(home)


def _stub_getent(tmp_path: Path, account_home: str) -> str:
    """PATH entry whose ``getent`` reports *account_home* ('' = no entry)."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    line = f"user:x:{os.getuid()}:{os.getgid()}::{account_home}:/bin/bash"
    body = f"echo '{line}'" if account_home else "exit 2"
    getent = bin_dir / "getent"
    getent.write_text(f"#!/bin/bash\n{body}\n")
    getent.chmod(0o755)
    return f"{bin_dir}:/usr/bin:/bin:/usr/local/bin"


def test_preflight_refuses_an_empty_home(home: Path) -> None:
    result = _bash(home, "preflight_protected_paths", HOME="")

    assert result.returncode != 0
    assert "REFUSING" in result.stderr and "HOME" in result.stderr, result.stderr


@pytest.mark.parametrize("fake_home", ["", "{home}"])
@pytest.mark.parametrize("suffix", ["/.cidx-server", "/.cidx-server/data", ""])
def test_the_account_home_from_the_password_database_is_protected(
    home: Path, fake_home: str, suffix: str
) -> None:
    """Only the guard predicate runs -- nothing is ever deleted."""
    account_home = pwd.getpwuid(os.getuid()).pw_dir
    result = _bash(
        home,
        f'safe_to_wipe LABEL "{account_home}{suffix}"',
        HOME=fake_home.format(home=home),
    )

    assert result.returncode != 0
    assert "REFUSING to wipe LABEL" in result.stderr, result.stderr


def test_a_stubbed_account_home_is_protected(home: Path, tmp_path: Path) -> None:
    account = tmp_path / "account"
    (account / ".cidx-server").mkdir(parents=True)
    path = _stub_getent(tmp_path, str(account))

    result = _bash(home, f'safe_to_wipe LABEL "{account}/.cidx-server"', PATH=path)

    assert result.returncode != 0
    assert "REFUSING" in result.stderr, result.stderr


def test_guard_refuses_when_the_account_database_has_no_home(
    home: Path, tmp_path: Path
) -> None:
    path = _stub_getent(tmp_path, "")

    result = _bash(home, f'safe_to_wipe LABEL "{home}/.tmp/anything"', PATH=path)

    assert result.returncode != 0
    assert "REFUSING" in result.stderr, result.stderr


@pytest.mark.parametrize("var", ["E2E_WORK_DIR", "E2E_SEED_CACHE_DIR"])
def test_preflight_refuses_a_work_or_seed_root_in_the_real_home(
    home: Path, var: str
) -> None:
    result = _bash(
        home, "preflight_protected_paths", **{var: str(home / ".cidx-server")}
    )

    assert result.returncode != 0
    assert "REFUSING" in result.stderr and var in result.stderr, result.stderr
    assert _sentinel_intact(home)


def test_copy_seed_repo_refuses_a_target_inside_the_real_home(
    home: Path, tmp_path: Path
) -> None:
    victim = home / ".cidx-server" / "markupsafe"
    victim.mkdir()
    (victim / "keep").write_text("keep")
    seed = tmp_path / "seed" / "markupsafe"
    seed.mkdir(parents=True)

    result = _bash(
        home,
        "copy_seed_repo markupsafe",
        E2E_WORK_DIR=str(home / ".cidx-server"),
        E2E_SEED_CACHE_DIR=str(tmp_path / "seed"),
    )

    assert result.returncode != 0
    assert "REFUSING" in result.stderr, result.stderr
    assert (victim / "keep").exists()
    assert _sentinel_intact(home)


# reset_test_environment also reaps daemons: stub that so no real process is
# ever touched; only the pytest-temp prune runs.
_NO_REAP = "_reap_tmp_test_daemons() { _REAP_COUNT=0; }"


def _pytest_dirs(base: Path, count: int) -> list:
    """pytest-0..N-1 with strictly increasing mtimes (pytest-0 oldest)."""
    dirs = []
    for i in range(count):
        d = base / f"pytest-{i}"
        d.mkdir(parents=True)
        (d / "sentinel").write_text("keep")
        os.utime(d, (1_000_000 + i, 1_000_000 + i))
        dirs.append(d)
    return dirs


def test_reset_prunes_old_pytest_dirs_after_a_passing_preflight(
    home: Path, tmp_path: Path
) -> None:
    base = tmp_path / "pytest-base"
    dirs = _pytest_dirs(base, 5)

    result = _bash(
        home,
        f"{_NO_REAP}\npreflight_protected_paths\nreset_test_environment",
        E2E_PYTEST_TEMP_BASE=str(base),
    )

    assert result.returncode == 0, result.stderr
    assert [d.exists() for d in dirs] == [False, False, True, True, True]


def test_reset_does_not_prune_after_a_failed_preflight(
    home: Path, tmp_path: Path
) -> None:
    base = tmp_path / "pytest-base"
    dirs = _pytest_dirs(base, 5)

    result = _bash(
        home,
        f"{_NO_REAP}\npreflight_protected_paths\nreset_test_environment",
        HOME="",
        E2E_PYTEST_TEMP_BASE=str(base),
    )

    assert "REFUSING" in result.stderr, result.stderr
    assert all(d.exists() for d in dirs)


def test_an_inherited_preflight_flag_does_not_authorise_the_prune(
    home: Path, tmp_path: Path
) -> None:
    base = tmp_path / "pytest-base"
    dirs = _pytest_dirs(base, 5)

    result = _bash(
        home,
        f"{_NO_REAP}\nreset_test_environment",
        E2E_PREFLIGHT_PASSED="yes",
        E2E_PYTEST_TEMP_BASE=str(base),
    )

    assert result.returncode == 0, result.stderr
    assert all(d.exists() for d in dirs)


def test_reset_skips_a_symlinked_pytest_base_into_the_protected_home(
    home: Path, tmp_path: Path
) -> None:
    victims = _pytest_dirs(home / ".cidx-server", 5)
    base = tmp_path / "pytest-base-link"
    base.symlink_to(home / ".cidx-server")

    result = _bash(
        home,
        f"{_NO_REAP}\npreflight_protected_paths\nreset_test_environment",
        E2E_PYTEST_TEMP_BASE=str(base),
    )

    assert result.returncode == 0, result.stderr
    assert "Skipping pytest temp prune" in result.stdout, result.stdout
    assert all((v / "sentinel").exists() for v in victims)
    assert _sentinel_intact(home)


def test_reset_skips_symlinked_pytest_entries(home: Path, tmp_path: Path) -> None:
    base = tmp_path / "pytest-base"
    _pytest_dirs(base, 4)
    target = home / ".cidx-server" / "linked"
    target.mkdir()
    (target / "keep").write_text("keep")
    link = base / "pytest-link"
    link.symlink_to(target)
    os.utime(link, (999_000, 999_000), follow_symlinks=False)  # the oldest entry

    result = _bash(
        home,
        f"{_NO_REAP}\npreflight_protected_paths\nreset_test_environment",
        E2E_PYTEST_TEMP_BASE=str(base),
    )

    assert result.returncode == 0, result.stderr
    assert link.is_symlink() and (target / "keep").exists()


def test_safe_to_wipe_accepts_a_scratch_dir(home: Path) -> None:
    result = _bash(home, f'safe_to_wipe LABEL "{home}/.tmp/anything"')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "target",
    [
        "/",
        "{home}",
        "{home}/.cidx-server",
        "{home}/.cidx-server/data",
        "{home}/.cidx-server/../.cidx-server",
        "{home}/.tmp",
        "{outside}",
    ],
)
def test_wipe_refuses_unsafe_targets(home: Path, tmp_path: Path, target: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    value = target.format(home=home, outside=outside)

    result = _bash(home, "wipe_client_server_home", E2E_CLIENT_SERVER_HOME=value)

    assert result.returncode != 0
    assert "REFUSING" in result.stderr, result.stderr
    assert _sentinel_intact(home)
    assert (outside / "keep").exists()


@pytest.mark.parametrize("scratch_root", ["{home}/.cidx-server", "{home}", "/"])
def test_wipe_refuses_when_the_scratch_root_itself_is_unsafe(
    home: Path, scratch_root: str
) -> None:
    client_home = home / ".cidx-server" / "client"
    client_home.mkdir()
    (client_home / "keep").write_text("keep")

    result = _bash(
        home,
        "wipe_client_server_home",
        E2E_SCRATCH_ROOT=scratch_root.format(home=home),
        E2E_CLIENT_SERVER_HOME=str(client_home),
    )

    assert result.returncode != 0
    assert "REFUSING" in result.stderr, result.stderr
    assert (client_home / "keep").exists()
    assert _sentinel_intact(home)


def test_wipe_refuses_a_shared_prefix_sibling_of_the_scratch_root(
    home: Path,
) -> None:
    sibling = home / ".tmpX" / "client"
    sibling.mkdir(parents=True)
    (sibling / "keep").write_text("keep")

    result = _bash(home, "wipe_client_server_home", E2E_CLIENT_SERVER_HOME=str(sibling))

    assert result.returncode != 0
    assert "REFUSING" in result.stderr, result.stderr
    assert (sibling / "keep").exists()


def test_wipe_recreates_an_empty_dir_inside_the_scratch_root(home: Path) -> None:
    client_home = home / ".tmp" / "client-home"
    client_home.mkdir()
    (client_home / "config.json").write_text("{}")

    result = _bash(
        home, "wipe_client_server_home", E2E_CLIENT_SERVER_HOME=str(client_home)
    )

    assert result.returncode == 0, result.stderr
    assert client_home.is_dir() and list(client_home.iterdir()) == []
    assert _sentinel_intact(home)


@pytest.mark.parametrize("var", ["CIDX_SERVER_DATA_DIR", "CIDX_DATA_DIR"])
@pytest.mark.parametrize("suffix", ["/.cidx-server", "/.cidx-server/data", ""])
def test_caller_env_pointing_at_the_real_home_is_refused(
    home: Path, var: str, suffix: str
) -> None:
    result = _bash(home, "check_caller_server_env", **{var: f"{home}{suffix}"})

    assert result.returncode != 0
    assert "REFUSING" in result.stderr and var in result.stderr, result.stderr


def test_caller_env_elsewhere_is_accepted(home: Path, tmp_path: Path) -> None:
    result = _bash(
        home,
        "check_caller_server_env",
        CIDX_SERVER_DATA_DIR=str(tmp_path / "srv"),
        CIDX_DATA_DIR=str(tmp_path / "data"),
    )
    assert result.returncode == 0, result.stderr


def test_run_phase_gives_each_phase_a_fresh_client_home(home: Path) -> None:
    client_home = home / ".tmp" / "client-home"
    client_home.mkdir()
    (client_home / "config.json").write_text("written by the previous phase")

    result = _bash(
        home,
        'run_phase 1 "CLI Standalone" tests/e2e/cli_standalone',
        E2E_CLIENT_SERVER_HOME=str(client_home),
        CIDX_SERVER_DATA_DIR=str(home / ".cidx-server"),
    )

    assert result.returncode == 0, result.stderr
    assert [p.name for p in client_home.iterdir()] == ["systemd-units"], (
        "previous phase's files survived"
    )
    assert f"SERVER_DIR={client_home}\n" in result.stdout
    assert f"DATA_DIR={client_home}\n" in result.stdout
    assert f"UNIT_DIR={client_home}/systemd-units\n" in result.stdout
    assert _parsed_stub(client_home / "systemd-units") == _stub_flags(
        DEFAULT_PHASE_SERVER_PORT
    )


def test_run_phase_refuses_an_unsafe_client_home(home: Path) -> None:
    result = _bash(
        home,
        'run_phase 1 "CLI Standalone" tests/e2e/cli_standalone',
        E2E_CLIENT_SERVER_HOME=str(home / ".cidx-server"),
    )

    assert result.returncode != 0
    assert "REFUSING" in result.stderr
    assert "SERVER_DIR=" not in result.stdout, "pytest ran with an unsafe home"
    assert _sentinel_intact(home)


def test_otel_subcheck_gets_a_fresh_client_home(home: Path) -> None:
    client_home = home / ".tmp" / "client-home"
    client_home.mkdir()
    (client_home / "stale").write_text("x")

    result = _bash(
        home,
        "run_otel_live_collector_subcheck",
        E2E_CLIENT_SERVER_HOME=str(client_home),
    )

    assert result.returncode == 0, result.stderr
    assert [p.name for p in client_home.iterdir()] == ["systemd-units"]
    assert f"SERVER_DIR={client_home}\n" in result.stdout
    assert f"DATA_DIR={client_home}\n" in result.stdout
    assert f"UNIT_DIR={client_home}/systemd-units\n" in result.stdout
    assert _parsed_stub(client_home / "systemd-units") == _stub_flags(
        DEFAULT_PHASE_SERVER_PORT
    )


def test_stub_unit_is_parsed_by_the_execstart_reader(
    home: Path, tmp_path: Path
) -> None:
    unit_dir = tmp_path / "units"

    result = _bash(home, f'write_stub_systemd_unit "{unit_dir}" {STUB_ROUND_TRIP_PORT}')

    assert result.returncode == 0, result.stderr
    assert (unit_dir / "cidx-server.service").is_file()
    assert _parsed_stub(unit_dir) == _stub_flags(STUB_ROUND_TRIP_PORT)


@pytest.mark.parametrize(
    "launcher, data_var, port_var, port",
    [
        ("start_phase4_server", "E2E_SERVER_DATA_DIR", "E2E_SERVER_PORT", PHASE4_PORT),
        (
            "start_fault_server",
            "E2E_FAULT_SERVER_DATA_DIR",
            "E2E_FAULT_SERVER_PORT",
            FAULT_PORT,
        ),
        ("start_pg_server", "E2E_PG_SERVER_DATA_DIR", "E2E_PG_SERVER_PORT", PG_PORT),
        (
            "start_siem_server",
            "E2E_SIEM_SERVER_DATA_DIR",
            "E2E_SIEM_SERVER_PORT",
            SIEM_PORT,
        ),
    ],
)
def test_live_servers_read_their_own_stub_unit(
    home: Path,
    tmp_path: Path,
    launcher: str,
    data_var: str,
    port_var: str,
    port: int,
) -> None:
    data_dir = tmp_path / "server-data"
    data_dir.mkdir()

    result = _bash(
        home, f"{launcher}\nwait", **{data_var: str(data_dir), port_var: str(port)}
    )

    assert result.returncode == 0, result.stderr
    log = (data_dir / "server.log").read_text()
    assert f"UNIT_DIR={data_dir}/systemd-units\n" in log, log
    assert _parsed_stub(data_dir / "systemd-units") == _stub_flags(port)
