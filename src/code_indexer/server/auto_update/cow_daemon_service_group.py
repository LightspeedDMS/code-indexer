"""Pure helpers for the CoW-daemon service-group membership self-heal.

Why the daemon user must be in the service group: cidx-server creates each
``activated-repos/<user>/`` directory itself (owner = service user, group =
the service user's primary group, mode 0o2775 -- see
``code_indexer.server.repositories.user_dir_permissions``). In cluster mode the
CoW storage daemon, running as its OWN OS user on the storage host, must then
create the clone ``<user>/<alias>`` inside it (and remove it on deactivation),
which needs write+search permission on that directory. Without membership the
daemon falls into the "other" class (r-x) and a brand-new user cannot activate
any repository. The group to join is the daemon's own configured
``service_group`` -- the same group it already grants clone ACLs to -- which
must therefore also be a group of the cidx service user.

These helpers do no privileged work; ``DeploymentExecutor`` resolves the
daemon identity through systemd and orchestrates ``usermod``/``restart``.
"""

import grp
import json
import pwd
import re
from pathlib import Path

COW_DAEMON_SERVICE_NAME = "cow-storage-daemon"

# Conservative POSIX-portable account name: never anything a shell or
# usermod could interpret beyond a plain login name.
_SAFE_ACCOUNT_NAME = re.compile(r"^[a-z_][a-z0-9_-]*$")


def read_daemon_service_group(config_path: Path) -> str:
    """Return the ``service_group`` from the co-located CoW daemon config.

    Raises:
        ValueError: config unreadable/malformed or ``service_group`` missing.
    """
    try:
        config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read CoW daemon config {config_path}: {exc}")
    service_group = config.get("service_group") if isinstance(config, dict) else None
    if not isinstance(service_group, str) or not service_group.strip():
        raise ValueError(f"CoW daemon config {config_path} has no service_group")
    return service_group.strip()


def validate_daemon_user(name: str) -> str:
    """Return ``name`` when it is a safe, existing, non-root account.

    Raises:
        ValueError: name invalid, account does not exist, or it is root.
    """
    if not _SAFE_ACCOUNT_NAME.match(name):
        raise ValueError(f"CoW daemon user name {name!r} is invalid")
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        raise ValueError(f"CoW daemon user {name!r} does not exist")
    if entry.pw_uid == 0:
        raise ValueError(f"CoW daemon user {name!r} is root (uid 0); refusing")
    return name


def account_name_for_uid(uid: int) -> str:
    """Return the passwd name for ``uid``; ValueError when there is none."""
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        raise ValueError(f"uid {uid} has no passwd entry")


def process_real_uid(proc_status_text: str) -> int:
    """Return the real uid from a /proc/<pid>/status text (``Uid:`` line)."""
    for line in proc_status_text.splitlines():
        label, _, values = line.partition(":")
        if label == "Uid" and values.split():
            return int(values.split()[0])
    raise ValueError("no Uid: line in process status")


def resolve_group_gid(group_name: str) -> int:
    """Return the gid of ``group_name``; ValueError when the group is unknown."""
    try:
        return grp.getgrnam(group_name).gr_gid
    except KeyError:
        raise ValueError(f"group '{group_name}' does not exist on this host")


def primary_group_name(user: str) -> str:
    """Return the name of ``user``'s primary group (its gid if unnamed)."""
    gid = pwd.getpwnam(user).pw_gid
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def user_in_group(user: str, group_name: str) -> bool:
    """True when ``user`` has ``group_name`` as primary or supplementary group."""
    group = grp.getgrnam(group_name)
    if user in group.gr_mem:
        return True
    try:
        return pwd.getpwnam(user).pw_gid == group.gr_gid
    except KeyError:
        return False


def process_has_gid(proc_status_text: str, gid: int) -> bool:
    """True when a /proc/<pid>/status text lists ``gid`` as a group of the
    process (its real/effective gid on the ``Gid:`` line, or any entry of the
    ``Groups:`` supplementary list)."""
    for line in proc_status_text.splitlines():
        label, _, values = line.partition(":")
        if label in ("Gid", "Groups") and str(gid) in values.split():
            return True
    return False
