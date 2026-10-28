# ^ coupled scripts/install-cidx-server.sh:install_service_limits with service_unit_limits.py; real bash via sudo shim, systemctl recorded; #2299

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Tuple

from code_indexer.server.auto_update.service_unit_limits import (
    ensure_conf_files,
    managed_conf_files,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "install-cidx-server.sh"
BASH_TIMEOUT_SECONDS = 60


def _run_installer(
    unit_dir: Path, log: Path, dry_run: bool
) -> subprocess.CompletedProcess:
    snippet = f"""
set -e
sudo() {{
    if [[ "$1" == "systemctl" ]]; then echo "$*" >> {str(log)!r}; return 0; fi
    "$@"
}}
export SYSTEMD_UNIT_DIR={str(unit_dir)!r}
source {str(SCRIPT)!r}
INSTALL_DIR={str(REPO_ROOT)!r}
DRY_RUN={"true" if dry_run else "false"}
install_service_limits
"""
    return subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        timeout=BASH_TIMEOUT_SECONDS,
    )


def _tree(root: Path) -> Dict[str, Tuple[str, int]]:
    return {
        str(p.relative_to(root)): (p.read_text(), p.stat().st_mode & 0o777)
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "systemctl.log"
    }


def test_installer_writes_the_same_files_as_the_self_heal(tmp_path: Path) -> None:
    installer_root = tmp_path / "installer" / "etc-systemd"
    healer_root = tmp_path / "healer" / "etc-systemd"
    (installer_root / "system").mkdir(parents=True)
    (healer_root / "system").mkdir(parents=True)
    log = tmp_path / "installer" / "systemctl.log"

    result = _run_installer(installer_root / "system", log, dry_run=False)
    assert result.returncode == 0, result.stderr

    files = managed_conf_files(REPO_ROOT, healer_root / "system", "cidx-server")
    ok = ensure_conf_files(
        files,
        lambda argv, stdin: subprocess.run(
            argv, input=stdin, capture_output=True, text=True
        ),
        lambda: True,
    )
    assert ok is True

    installed = _tree(installer_root)
    assert installed == _tree(healer_root)
    assert len(installed) == 2
    assert log.read_text().splitlines() == ["systemctl daemon-reload"]


def test_installer_dry_run_writes_nothing(tmp_path: Path) -> None:
    unit_dir = tmp_path / "etc-systemd" / "system"
    unit_dir.mkdir(parents=True)
    log = tmp_path / "systemctl.log"

    result = _run_installer(unit_dir, log, dry_run=True)

    assert result.returncode == 0, result.stderr
    assert "20-cidx-limits.conf" in result.stdout
    assert _tree(tmp_path / "etc-systemd") == {}
    assert not log.exists()


def test_main_installs_service_limits_after_the_unit() -> None:
    source = SCRIPT.read_text()
    main_body = source[source.index("\nmain() {") :]
    unit_pos = main_body.index("\n    create_systemd_service\n")
    limits_pos = main_body.index("\n    install_service_limits\n")
    start_pos = main_body.index("\n    start_and_verify_server\n")
    assert unit_pos < limits_pos < start_pos
