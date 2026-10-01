"""Tests for install-cidx-server.sh's ensure_cow_daemon_service_group_membership().

Fresh-install twin of DeploymentExecutor._ensure_cow_daemon_user_in_service_group
(see tests/unit/server/auto_update/test_cow_daemon_service_group_membership.py):
on the CoW daemon host, the daemon's OS user must be a member of the daemon's
configured ``service_group`` so it can create clones inside the service-owned,
group-writable ``activated-repos/<user>/`` directories, and the RUNNING daemon
must carry that gid.

The ACTUAL bash function is sourced from the real script (``main`` never runs
thanks to the BASH_SOURCE guard) and executed against a REAL daemon config in
a tmp dir, the REAL host passwd/group databases (``getent``/``id``) and a REAL
tmp /proc tree (``CIDX_PROC_ROOT``). Only systemd and privileged mutations are
intercepted: a ``systemctl()`` shell override answers ``systemctl show``
(unit ``User=`` / ``MainPID``) from env vars, and a ``sudo()`` override records
``usermod``/``systemctl`` invocations instead of executing them while running
every other command (test/python3 reads) for real.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import re
import subprocess
from pathlib import Path
from typing import List, Optional

import pytest

_REPO_ROOT_LEVELS_ABOVE_THIS_FILE = 3
_BASH_INVOCATION_TIMEOUT_SECONDS = 30
_DAEMON_PID = 4242
_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[_REPO_ROOT_LEVELS_ABOVE_THIS_FILE]
    / "scripts"
    / "install-cidx-server.sh"
)

pytestmark = pytest.mark.skipif(
    not _SCRIPT_PATH.exists(), reason="install-cidx-server.sh not found"
)


def _current_user() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def _primary_group() -> grp.struct_group:
    return grp.getgrgid(pwd.getpwuid(os.geteuid()).pw_gid)


def _group_user_is_not_in() -> Optional[str]:
    user = _current_user()
    for group in grp.getgrall():
        if group.gr_gid != _primary_group().gr_gid and user not in group.gr_mem:
            return group.gr_name
    return None


def _outside_daemon_account() -> pwd.struct_passwd:
    group = _primary_group()
    for entry in pwd.getpwall():
        if (
            entry.pw_uid != 0
            and entry.pw_name != _current_user()
            and _NAME_RE.match(entry.pw_name)
            and entry.pw_name not in group.gr_mem
            and entry.pw_gid != group.gr_gid
        ):
            return entry
    pytest.skip("host has no other non-root account outside the test group")


def _write_config(tmp_path: Path, service_group: Optional[str]) -> Path:
    config = {"base_path": str(tmp_path / "storage")}
    if service_group is not None:
        config["service_group"] = service_group
    path = tmp_path / "daemon-config.json"
    path.write_text(json.dumps(config))
    return path


def _write_proc(tmp_path: Path, uid: int, groups: List[int]) -> Path:
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / str(_DAEMON_PID)
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "status").write_text(
        f"Name:\tpython3\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        f"Gid:\t99999\t99999\t99999\t99999\n"
        f"Groups:\t{' '.join(str(g) for g in groups)} \n"
    )
    return proc_root


def _run(
    config_path: Path,
    proc_root: Path,
    unit_user: str,
    main_pid: int = _DAEMON_PID,
    dry_run: bool = False,
) -> subprocess.CompletedProcess:
    bash_snippet = f"""
set -e
sudo() {{
    case "$1" in
        usermod|systemctl) echo "PRIVILEGED: $*"; return 0 ;;
    esac
    "$@"
}}
systemctl() {{
    if [[ "$1" == "show" ]]; then
        case "$3" in
            User) echo {unit_user!r} ;;
            MainPID) echo {str(main_pid)!r} ;;
        esac
        return 0
    fi
    echo "UNEXPECTED systemctl $*" >&2
    return 1
}}
export CIDX_COW_DAEMON_HOST_CONFIG_PATH={str(config_path)!r}
export CIDX_PROC_ROOT={str(proc_root)!r}
source {str(_SCRIPT_PATH)!r}
DRY_RUN={"true" if dry_run else "false"}
ensure_cow_daemon_service_group_membership
"""
    return subprocess.run(
        ["bash", "-c", bash_snippet],
        capture_output=True,
        text=True,
        timeout=_BASH_INVOCATION_TIMEOUT_SECONDS,
    )


def _privileged(result: subprocess.CompletedProcess) -> List[str]:
    return [
        line.removeprefix("PRIVILEGED: ")
        for line in result.stdout.splitlines()
        if line.startswith("PRIVILEGED: ")
    ]


def test_adds_membership_and_restarts_daemon_when_missing(tmp_path: Path) -> None:
    group = _primary_group().gr_name
    daemon = _outside_daemon_account()
    proc_root = _write_proc(tmp_path, daemon.pw_uid, [])

    result = _run(_write_config(tmp_path, group), proc_root, daemon.pw_name)

    assert result.returncode == 0, result.stderr
    assert _privileged(result) == [
        f"usermod -aG {group} {daemon.pw_name}",
        "systemctl restart cow-storage-daemon",
    ]


def test_daemon_user_falls_back_to_running_process_uid(tmp_path: Path) -> None:
    group = _primary_group().gr_name
    daemon = _outside_daemon_account()
    proc_root = _write_proc(tmp_path, daemon.pw_uid, [])

    result = _run(_write_config(tmp_path, group), proc_root, "")

    assert result.returncode == 0, result.stderr
    assert f"usermod -aG {group} {daemon.pw_name}" in _privileged(result)


def test_noop_when_member_and_running_daemon_has_gid(tmp_path: Path) -> None:
    group = _primary_group()
    proc_root = _write_proc(tmp_path, os.geteuid(), [group.gr_gid])

    result = _run(_write_config(tmp_path, group.gr_name), proc_root, _current_user())

    assert result.returncode == 0, result.stderr
    assert _privileged(result) == []


def test_rerun_restarts_when_member_but_running_daemon_lacks_gid(
    tmp_path: Path,
) -> None:
    """A prior run added the membership but the restart failed: a re-run
    must restart (and must not repeat usermod)."""
    group = _primary_group()
    proc_root = _write_proc(tmp_path, os.geteuid(), [])

    result = _run(_write_config(tmp_path, group.gr_name), proc_root, _current_user())

    assert result.returncode == 0, result.stderr
    assert _privileged(result) == ["systemctl restart cow-storage-daemon"]


def test_no_restart_when_daemon_not_running(tmp_path: Path) -> None:
    group = _primary_group().gr_name
    daemon = _outside_daemon_account()

    result = _run(
        _write_config(tmp_path, group), tmp_path / "proc", daemon.pw_name, main_pid=0
    )

    assert result.returncode == 0, result.stderr
    assert _privileged(result) == [f"usermod -aG {group} {daemon.pw_name}"]


def test_skips_when_not_daemon_host(tmp_path: Path) -> None:
    result = _run(tmp_path / "absent.json", tmp_path, _current_user())

    assert result.returncode == 0, result.stderr
    assert _privileged(result) == []


@pytest.mark.parametrize(
    "unit_user,reason",
    [
        ("root", "root"),
        ("no-such-user-for-cidx-test", "does not exist"),
        ("Bad;Name", "invalid"),
    ],
)
def test_unsafe_daemon_identity_logs_error_and_skips(
    tmp_path: Path, unit_user: str, reason: str
) -> None:
    proc_root = _write_proc(tmp_path, os.geteuid(), [])

    result = _run(
        _write_config(tmp_path, _primary_group().gr_name), proc_root, unit_user
    )

    assert result.returncode == 0, result.stderr
    assert _privileged(result) == []
    assert "ERROR" in result.stderr and reason in result.stderr


def test_root_resolved_from_running_process_is_rejected(tmp_path: Path) -> None:
    proc_root = _write_proc(tmp_path, 0, [])

    result = _run(_write_config(tmp_path, _primary_group().gr_name), proc_root, "")

    assert result.returncode == 0, result.stderr
    assert _privileged(result) == []
    assert "ERROR" in result.stderr


def test_unresolvable_daemon_identity_logs_error_and_skips(tmp_path: Path) -> None:
    result = _run(
        _write_config(tmp_path, _primary_group().gr_name), tmp_path, "", main_pid=0
    )

    assert result.returncode == 0, result.stderr
    assert _privileged(result) == []
    assert "ERROR" in result.stderr


@pytest.mark.parametrize(
    "config_text", [json.dumps({"base_path": "/srv/x"}), "{", "not json"]
)
def test_fails_loudly_on_unusable_config(tmp_path: Path, config_text: str) -> None:
    config = tmp_path / "daemon-config.json"
    config.write_text(config_text)

    result = _run(config, tmp_path, _current_user())

    assert result.returncode != 0
    assert _privileged(result) == []
    assert "ERROR" in result.stderr


def test_fails_loudly_when_group_unknown(tmp_path: Path) -> None:
    result = _run(
        _write_config(tmp_path, "no-such-group-for-cidx-test"),
        tmp_path,
        _current_user(),
    )

    assert result.returncode != 0
    assert _privileged(result) == []
    assert "no-such-group-for-cidx-test" in result.stderr


def test_fails_loudly_when_service_group_not_a_group_of_cidx_user(
    tmp_path: Path,
) -> None:
    foreign = _group_user_is_not_in()
    if foreign is None:
        pytest.skip("host has no group the test user is outside of")

    result = _run(_write_config(tmp_path, foreign), tmp_path, _current_user())

    assert result.returncode != 0
    assert _privileged(result) == []
    assert foreign in result.stderr and _primary_group().gr_name in result.stderr


def test_fails_loudly_when_service_group_is_only_supplementary(
    tmp_path: Path,
) -> None:
    """New user dirs carry the service user's PRIMARY gid, so a group the
    service user merely belongs to must be rejected."""
    user = _current_user()
    supplementary = [
        g.gr_name
        for g in grp.getgrall()
        if user in g.gr_mem and g.gr_gid != _primary_group().gr_gid
    ]
    if not supplementary:
        pytest.skip("test user has no supplementary group")

    result = _run(_write_config(tmp_path, supplementary[0]), tmp_path, user)

    assert result.returncode != 0
    assert _privileged(result) == []
    assert "ERROR" in result.stderr
    assert supplementary[0] in result.stderr
    assert _primary_group().gr_name in result.stderr


def test_dry_run_prints_without_executing(tmp_path: Path) -> None:
    group = _primary_group().gr_name
    daemon = _outside_daemon_account()
    proc_root = _write_proc(tmp_path, daemon.pw_uid, [])

    result = _run(
        _write_config(tmp_path, group), proc_root, daemon.pw_name, dry_run=True
    )

    assert result.returncode == 0, result.stderr
    assert _privileged(result) == []
    assert f"[dry-run] sudo usermod -aG {group} {daemon.pw_name}" in result.stdout
    assert "[dry-run] sudo systemctl restart cow-storage-daemon" in result.stdout


def test_main_invokes_function_for_cow_daemon_backend() -> None:
    """Anti-orphan guard: main() must call the step inside its cow-daemon
    branch, not merely define it."""
    script = _SCRIPT_PATH.read_text()
    main_body = script[script.index("\nmain() {") :]
    branch = main_body[
        main_body.index(
            'if [[ "${CLONE_BACKEND}" == "cow-daemon" ]]; then\n'
            "        resolve_cow_daemon_storage_path"
        ) :
    ]
    branch = branch[: branch.index("fi\n")]
    assert "ensure_cow_daemon_service_group_membership" in branch
