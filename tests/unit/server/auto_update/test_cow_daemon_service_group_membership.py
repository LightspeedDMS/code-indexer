"""Tests for DeploymentExecutor._ensure_cow_daemon_user_in_service_group().

The CoW storage daemon creates per-user clones inside
``activated-repos/<user>/``, a directory owned by the cidx-server service
user (group = the service user's primary group, mode 0o2775). The daemon runs
as a different OS user, so it can only write there through the group bits:
the daemon user must be a member of the daemon's configured ``service_group``,
that group must be a group of the cidx service user, and the RUNNING daemon
process must carry the gid (supplementary groups are fixed at process start).

Runs only on the daemon-host node (the daemon's own config file exists) and
converges idempotently:
  1. daemon identity = the unit's ``User=`` (``systemctl show``), else the
     running MainPID's real uid; validated (account exists, not uid 0,
     conservative name) -- never guessed;
  2. ``service_group`` must be a group of the cidx service user (from the
     cidx-server unit's ``User=``) -- ERROR naming both groups otherwise;
  3. account not in group -> ``sudo usermod -aG <group> <user>``;
  4. running process lacks the gid -> ``sudo systemctl restart cow-storage-daemon``.

Test strategy: the daemon config and the cidx-server unit are REAL files in a
tmp dir; users/groups are resolved against the REAL host passwd/group
databases; /proc is a REAL tmp tree. Only the privileged/systemd boundary
(``_run_systemd_op_with_retry``) is faked, the same boundary every sibling
``_ensure_*`` test in this package fakes.
"""

import grp
import inspect
import json
import logging
import os
import pwd
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

DAEMON_PID = 4242
_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")


def _current_user() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def _primary_gid() -> int:
    return pwd.getpwuid(os.geteuid()).pw_gid


def _primary_group() -> grp.struct_group:
    return grp.getgrgid(_primary_gid())


def _group_user_is_not_in() -> Optional[grp.struct_group]:
    user = _current_user()
    for group in grp.getgrall():
        if group.gr_gid != _primary_gid() and user not in group.gr_mem:
            return group
    return None


def _other_account_outside(group: grp.struct_group) -> Optional[pwd.struct_passwd]:
    """A real, non-root, validly named account that is NOT in ``group``."""
    for entry in pwd.getpwall():
        if (
            entry.pw_uid != 0
            and entry.pw_name != _current_user()
            and _NAME_RE.match(entry.pw_name)
            and entry.pw_name not in group.gr_mem
            and entry.pw_gid != group.gr_gid
        ):
            return entry
    return None


def _write_daemon_config(tmp_path: Path, service_group: Optional[str]) -> Path:
    config: Dict[str, str] = {"base_path": str(tmp_path / "storage")}
    if service_group is not None:
        config["service_group"] = service_group
    path = tmp_path / "cow-daemon-config.json"
    path.write_text(json.dumps(config))
    return path


def _write_server_unit(tmp_path: Path, user: Optional[str]) -> Path:
    unit = tmp_path / "cidx-server.service"
    user_line = f"User={user}\n" if user else ""
    unit.write_text(f"[Service]\nType=simple\n{user_line}ExecStart=/bin/true\n")
    return unit


def _write_proc_status(tmp_path: Path, groups: List[int], uid: int = 0) -> Path:
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / str(DAEMON_PID)
    pid_dir.mkdir(parents=True, exist_ok=True)
    gid = 99999
    (pid_dir / "status").write_text(
        f"Name:\tpython3\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        f"Gid:\t{gid}\t{gid}\t{gid}\t{gid}\n"
        f"Groups:\t{' '.join(str(g) for g in groups)} \n"
    )
    return proc_root


class _FakeSystemd:
    """Records every privileged command; answers ``systemctl show``."""

    def __init__(
        self, unit_user: str, main_pid: int = DAEMON_PID, fail_on: str = ""
    ) -> None:
        self.commands: List[List[str]] = []
        self._unit_user = unit_user
        self._main_pid = main_pid
        self._fail_on = fail_on

    def __call__(self, cmd: list, **kwargs) -> subprocess.CompletedProcess:
        self.commands.append(list(cmd))
        if self._fail_on and self._fail_on in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="denied")
        if "show" in cmd and "User" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{self._unit_user}\n")
        if "show" in cmd and "MainPID" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{self._main_pid}\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def mutations(self) -> List[List[str]]:
        return [c for c in self.commands if "show" not in c]


@pytest.fixture()
def executor() -> DeploymentExecutor:
    return DeploymentExecutor(repo_path=Path("/test/repo"), service_name="cidx-server")


def _run(
    executor: DeploymentExecutor,
    fake: _FakeSystemd,
    config_path: Path,
    proc_root: Path,
    server_unit: Path,
) -> bool:
    with patch.object(executor, "_run_systemd_op_with_retry", side_effect=fake):
        return executor._ensure_cow_daemon_user_in_service_group(
            daemon_config_path=config_path,
            proc_root=proc_root,
            server_unit_path=server_unit,
        )


def _outside_daemon_account() -> pwd.struct_passwd:
    account = _other_account_outside(_primary_group())
    if account is None:
        pytest.skip("host has no other non-root account outside the test group")
    return account


def test_adds_membership_and_restarts_daemon_when_missing(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    group = _primary_group()
    daemon = _outside_daemon_account()
    config = _write_daemon_config(tmp_path, group.gr_name)
    proc_root = _write_proc_status(tmp_path, [], uid=daemon.pw_uid)
    fake = _FakeSystemd(unit_user=daemon.pw_name)

    result = _run(
        executor, fake, config, proc_root, _write_server_unit(tmp_path, _current_user())
    )

    assert result is True
    assert fake.mutations() == [
        ["sudo", "usermod", "-aG", group.gr_name, daemon.pw_name],
        ["sudo", "systemctl", "restart", "cow-storage-daemon"],
    ]


def test_daemon_user_falls_back_to_running_process_uid(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    group = _primary_group()
    daemon = _outside_daemon_account()
    config = _write_daemon_config(tmp_path, group.gr_name)
    proc_root = _write_proc_status(tmp_path, [], uid=daemon.pw_uid)
    fake = _FakeSystemd(unit_user="")

    result = _run(
        executor, fake, config, proc_root, _write_server_unit(tmp_path, _current_user())
    )

    assert result is True
    assert ["sudo", "usermod", "-aG", group.gr_name, daemon.pw_name] in fake.commands


def test_noop_when_already_member_and_process_has_gid(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    group = _primary_group()
    config = _write_daemon_config(tmp_path, group.gr_name)
    proc_root = _write_proc_status(tmp_path, [group.gr_gid], uid=os.geteuid())
    unit = _write_server_unit(tmp_path, _current_user())
    fake = _FakeSystemd(unit_user=_current_user())

    first = _run(executor, fake, config, proc_root, unit)
    second = _run(executor, fake, config, proc_root, unit)

    assert first is True and second is True
    assert fake.mutations() == [], "converged host must see zero mutations"


def test_restarts_only_when_member_but_running_process_lacks_gid(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    """usermod landed on an earlier run but the restart did not: the next
    run must restart without repeating usermod."""
    group = _primary_group()
    config = _write_daemon_config(tmp_path, group.gr_name)
    proc_root = _write_proc_status(tmp_path, [], uid=os.geteuid())
    fake = _FakeSystemd(unit_user=_current_user())

    result = _run(
        executor, fake, config, proc_root, _write_server_unit(tmp_path, _current_user())
    )

    assert result is True
    assert fake.mutations() == [["sudo", "systemctl", "restart", "cow-storage-daemon"]]


def test_no_restart_when_daemon_not_running(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    group = _primary_group()
    daemon = _outside_daemon_account()
    config = _write_daemon_config(tmp_path, group.gr_name)
    fake = _FakeSystemd(unit_user=daemon.pw_name, main_pid=0)

    result = _run(
        executor,
        fake,
        config,
        tmp_path / "no-proc",
        _write_server_unit(tmp_path, _current_user()),
    )

    assert result is True
    assert fake.mutations() == [
        ["sudo", "usermod", "-aG", group.gr_name, daemon.pw_name]
    ]


def test_noop_on_nodes_without_daemon_config(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    fake = _FakeSystemd(unit_user=_current_user())
    unit = _write_server_unit(tmp_path, _current_user())
    assert _run(executor, fake, tmp_path / "absent.json", tmp_path, unit) is True
    assert fake.commands == []


@pytest.mark.parametrize(
    "unit_user,reason",
    [
        ("root", "root"),
        ("no-such-user-for-cidx-test", "does not exist"),
        ("Bad;Name", "invalid"),
    ],
)
def test_rejects_unsafe_daemon_identity(
    executor: DeploymentExecutor,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    unit_user: str,
    reason: str,
) -> None:
    config = _write_daemon_config(tmp_path, _primary_group().gr_name)
    proc_root = _write_proc_status(tmp_path, [], uid=os.geteuid())
    fake = _FakeSystemd(unit_user=unit_user)

    with caplog.at_level(logging.ERROR):
        result = _run(
            executor,
            fake,
            config,
            proc_root,
            _write_server_unit(tmp_path, _current_user()),
        )

    assert result is False
    assert fake.mutations() == []
    assert any(
        r.levelno == logging.ERROR and reason in r.getMessage() for r in caplog.records
    )


def test_rejects_root_resolved_from_running_process(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    config = _write_daemon_config(tmp_path, _primary_group().gr_name)
    proc_root = _write_proc_status(tmp_path, [], uid=0)
    fake = _FakeSystemd(unit_user="")

    result = _run(
        executor, fake, config, proc_root, _write_server_unit(tmp_path, _current_user())
    )

    assert result is False
    assert fake.mutations() == []


def test_unresolvable_when_unit_has_no_user_and_daemon_not_running(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    config = _write_daemon_config(tmp_path, _primary_group().gr_name)
    fake = _FakeSystemd(unit_user="", main_pid=0)

    result = _run(
        executor, fake, config, tmp_path, _write_server_unit(tmp_path, _current_user())
    )

    assert result is False
    assert fake.mutations() == []


def test_errors_when_service_group_is_not_a_group_of_cidx_service_user(
    executor: DeploymentExecutor, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    foreign = _group_user_is_not_in()
    if foreign is None:
        pytest.skip("host has no group the test user is outside of")
    config = _write_daemon_config(tmp_path, foreign.gr_name)
    proc_root = _write_proc_status(tmp_path, [], uid=os.geteuid())
    fake = _FakeSystemd(unit_user=_current_user())

    with caplog.at_level(logging.ERROR):
        result = _run(
            executor,
            fake,
            config,
            proc_root,
            _write_server_unit(tmp_path, _current_user()),
        )

    assert result is False
    assert fake.mutations() == []
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(
        foreign.gr_name in m and _primary_group().gr_name in m for m in messages
    ), messages


def _supplementary_only_group() -> grp.struct_group:
    user = _current_user()
    for group in grp.getgrall():
        if user in group.gr_mem and group.gr_gid != _primary_gid():
            return group
    pytest.skip("test user has no supplementary group")


def test_errors_when_service_group_is_only_a_supplementary_group_of_cidx_user(
    executor: DeploymentExecutor, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """New user dirs carry the service user's PRIMARY gid, so a group the
    service user merely belongs to would not give the daemon write access."""
    supplementary = _supplementary_only_group()
    config = _write_daemon_config(tmp_path, supplementary.gr_name)
    proc_root = _write_proc_status(tmp_path, [], uid=os.geteuid())
    fake = _FakeSystemd(unit_user=_current_user())

    with caplog.at_level(logging.ERROR):
        result = _run(
            executor,
            fake,
            config,
            proc_root,
            _write_server_unit(tmp_path, _current_user()),
        )

    assert result is False
    assert fake.mutations() == []
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(
        supplementary.gr_name in m and _primary_group().gr_name in m for m in messages
    ), messages


def test_primary_group_match_is_accepted(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    group = _primary_group()
    config = _write_daemon_config(tmp_path, group.gr_name)
    proc_root = _write_proc_status(tmp_path, [group.gr_gid], uid=os.geteuid())
    fake = _FakeSystemd(unit_user=_current_user())

    result = _run(
        executor, fake, config, proc_root, _write_server_unit(tmp_path, _current_user())
    )

    assert result is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_errors_and_skips_when_daemon_config_stat_fails(
    executor: DeploymentExecutor, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A permission/I-O error on the daemon config must not look like
    'daemon absent': ERROR + skip, never a silent no-op."""
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    config = _write_daemon_config(locked_dir, _primary_group().gr_name)
    os.chmod(locked_dir, 0)
    fake = _FakeSystemd(unit_user=_current_user())
    try:
        with caplog.at_level(logging.ERROR):
            result = _run(
                executor,
                fake,
                config,
                tmp_path,
                _write_server_unit(tmp_path, _current_user()),
            )
    finally:
        os.chmod(locked_dir, 0o700)

    assert result is False
    assert fake.commands == []
    assert any(
        r.levelno == logging.ERROR and "DEPLOY-GENERAL-224" in r.getMessage()
        for r in caplog.records
    )


def test_errors_when_cidx_server_unit_has_no_user(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    config = _write_daemon_config(tmp_path, _primary_group().gr_name)
    fake = _FakeSystemd(unit_user=_current_user())

    result = _run(executor, fake, config, tmp_path, _write_server_unit(tmp_path, None))

    assert result is False
    assert fake.mutations() == []


@pytest.mark.parametrize(
    "config_text",
    [json.dumps({"base_path": "/srv/x"}), json.dumps({"service_group": " "}), "{"],
)
def test_errors_without_valid_service_group(
    executor: DeploymentExecutor,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    config_text: str,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(config_text)
    fake = _FakeSystemd(unit_user=_current_user())

    with caplog.at_level(logging.ERROR):
        result = _run(
            executor,
            fake,
            config_path,
            tmp_path,
            _write_server_unit(tmp_path, _current_user()),
        )

    assert result is False
    assert fake.commands == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_fails_when_service_group_unknown_on_host(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    config = _write_daemon_config(tmp_path, "no-such-group-for-cidx-test")
    fake = _FakeSystemd(unit_user=_current_user())

    result = _run(
        executor, fake, config, tmp_path, _write_server_unit(tmp_path, _current_user())
    )

    assert result is False
    assert fake.commands == []


def test_fails_and_skips_restart_when_usermod_fails(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    group = _primary_group()
    daemon = _outside_daemon_account()
    config = _write_daemon_config(tmp_path, group.gr_name)
    proc_root = _write_proc_status(tmp_path, [], uid=daemon.pw_uid)
    fake = _FakeSystemd(unit_user=daemon.pw_name, fail_on="usermod")

    result = _run(
        executor, fake, config, proc_root, _write_server_unit(tmp_path, _current_user())
    )

    assert result is False
    assert ["sudo", "systemctl", "restart", "cow-storage-daemon"] not in fake.commands


def test_fails_when_restart_fails(executor: DeploymentExecutor, tmp_path: Path) -> None:
    group = _primary_group()
    config = _write_daemon_config(tmp_path, group.gr_name)
    proc_root = _write_proc_status(tmp_path, [], uid=os.geteuid())
    fake = _FakeSystemd(unit_user=_current_user(), fail_on="restart")

    result = _run(
        executor, fake, config, proc_root, _write_server_unit(tmp_path, _current_user())
    )

    assert result is False


def test_fails_when_systemctl_show_fails(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    config = _write_daemon_config(tmp_path, _primary_group().gr_name)
    fake = _FakeSystemd(unit_user=_current_user(), fail_on="show")

    result = _run(
        executor, fake, config, tmp_path, _write_server_unit(tmp_path, _current_user())
    )

    assert result is False
    assert fake.mutations() == []


@pytest.mark.parametrize(
    "status_text,gid,expected",
    [
        ("Gid:\t10\t10\t10\t10\nGroups:\t20 30 \n", 30, True),
        ("Gid:\t10\t10\t10\t10\nGroups:\t20 30 \n", 10, True),
        ("Gid:\t10\t10\t10\t10\nGroups:\t20 300 \n", 30, False),
        ("Gid:\t10\t10\t10\t10\nGroups:\t\n", 3, False),
    ],
)
def test_process_has_gid_parses_proc_status(
    status_text: str, gid: int, expected: bool
) -> None:
    from code_indexer.server.auto_update.cow_daemon_service_group import (
        process_has_gid,
    )

    assert process_has_gid(status_text, gid) is expected


def test_process_real_uid_requires_uid_line() -> None:
    from code_indexer.server.auto_update.cow_daemon_service_group import (
        process_real_uid,
    )

    assert process_real_uid("Uid:\t1234\t1234\t1234\t1234\n") == 1234
    with pytest.raises(ValueError):
        process_real_uid("Name:\tpython3\n")


def test_user_in_group_supplementary_member() -> None:
    from code_indexer.server.auto_update.cow_daemon_service_group import (
        user_in_group,
    )

    user = _current_user()
    supplementary = [
        g for g in grp.getgrall() if user in g.gr_mem and g.gr_gid != _primary_gid()
    ]
    if not supplementary:
        pytest.skip("test user has no supplementary group")
    assert user_in_group(user, supplementary[0].gr_name) is True


def test_user_in_group_unknown_user_is_false() -> None:
    from code_indexer.server.auto_update.cow_daemon_service_group import (
        user_in_group,
    )

    assert (
        user_in_group("no-such-user-for-cidx-test", _primary_group().gr_name) is False
    )


def test_execute_wires_cow_daemon_service_group_self_heal() -> None:
    source = inspect.getsource(DeploymentExecutor.execute)
    assert "_ensure_cow_daemon_user_in_service_group()" in source


def test_account_name_for_uid_without_passwd_entry_raises() -> None:
    from code_indexer.server.auto_update.cow_daemon_service_group import (
        account_name_for_uid,
    )

    unused_uid = 2**31 - 7
    try:
        pwd.getpwuid(unused_uid)
        pytest.skip("uid unexpectedly assigned on this host")
    except KeyError:
        pass
    with pytest.raises(ValueError):
        account_name_for_uid(unused_uid)
