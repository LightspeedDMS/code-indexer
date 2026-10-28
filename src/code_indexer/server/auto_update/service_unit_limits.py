# ^ see docs/server/auto-update.md; #2299 cidx-server limits drop-in and systemd-coredump drop-in, rendered verbatim from templates/
# ^ coupled with scripts/install-cidx-server.sh:install_service_limits; same templates, names and paths
# ^ external systemd-coredump reads coredump.conf.d at every crash => no reload; MaxUse/KeepFree are cleanup targets, not hard caps

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

TEMPLATE_SUBDIR = Path("src/code_indexer/server/auto_update/templates")
SERVER_LIMITS_TEMPLATE = "cidx-server-limits.conf"
COREDUMP_TEMPLATE = "cidx-server-coredump.conf"
# ^ order 10-nofile.conf (interim hand-made drop-in) before this name; the later drop-in's Limit* wins
SERVER_LIMITS_DROPIN_NAME = "20-cidx-limits.conf"
COREDUMP_DROPIN_NAME = "50-cidx-server.conf"
COREDUMP_CONF_DIR_NAME = "coredump.conf.d"
CONF_FILE_MODE = "0644"
CONF_ENCODING = "utf-8"
ERROR_CODE = "DEPLOY-GENERAL-230"
MAX_STDERR_CHARS = 200

RunPrivileged = Callable[[List[str], Optional[str]], subprocess.CompletedProcess]


@dataclass(frozen=True)
class ManagedConfFile:
    path: Path
    content: str
    needs_daemon_reload: bool


class ConfWriteError(RuntimeError):
    pass


def managed_conf_files(
    repo_path: Path, unit_dir: Path, service_name: str
) -> Tuple[ManagedConfFile, ...]:
    templates = repo_path / TEMPLATE_SUBDIR
    # ^ why unit_dir.parent, not a fixed /etc path; every write then follows SYSTEMD_UNIT_DIR, which tests point at scratch
    coredump_conf_dir = unit_dir.parent / COREDUMP_CONF_DIR_NAME
    return (
        ManagedConfFile(
            unit_dir / f"{service_name}.service.d" / SERVER_LIMITS_DROPIN_NAME,
            (templates / SERVER_LIMITS_TEMPLATE).read_text(encoding=CONF_ENCODING),
            True,
        ),
        ManagedConfFile(
            coredump_conf_dir / COREDUMP_DROPIN_NAME,
            (templates / COREDUMP_TEMPLATE).read_text(encoding=CONF_ENCODING),
            False,
        ),
    )


def _is_current(conf: ManagedConfFile) -> bool:
    try:
        return conf.path.read_text(encoding=CONF_ENCODING) == conf.content
    except (FileNotFoundError, UnicodeDecodeError):
        return False


def _write(conf: ManagedConfFile, run_privileged: RunPrivileged) -> None:
    # ^ why temp + mv in the same dir, not tee on the target; systemd never reads a half-written .conf
    tmp = conf.path.parent / f".{conf.path.name}.tmp"
    steps: Sequence[Tuple[List[str], Optional[str]]] = (
        (["mkdir", "-p", str(conf.path.parent)], None),
        (["tee", str(tmp)], conf.content),
        (["chmod", CONF_FILE_MODE, str(tmp)], None),
        (["mv", "-f", str(tmp), str(conf.path)], None),
    )
    for argv, stdin in steps:
        result = run_privileged(argv, stdin)
        if result.returncode != 0:
            raise ConfWriteError(
                f"{argv[0]} failed for {conf.path} (rc={result.returncode}): "
                f"{(result.stderr or '')[:MAX_STDERR_CHARS]}"
            )


# ^ invariant True only when every file is current or rewritten and any needed reload succeeded; failures are logged, never raised
def ensure_conf_files(
    files: Sequence[ManagedConfFile],
    run_privileged: RunPrivileged,
    reload_daemon: Callable[[], bool],
) -> bool:
    all_current = True
    reload_needed = False
    for conf in files:
        try:
            if _is_current(conf):
                continue
            _write(conf, run_privileged)
        except Exception as exc:
            all_current = False
            logger.warning(
                format_error_log(
                    ERROR_CODE,
                    f"Could not write {conf.path}: {type(exc).__name__}: {exc}",
                )
            )
            continue
        logger.info("Wrote managed systemd config %s", conf.path)
        reload_needed = reload_needed or conf.needs_daemon_reload
    if reload_needed and not reload_daemon():
        all_current = False
    return all_current
