"""Stub ``cidx-server.service`` for test-owned e2e servers (Bug #1996).

A deployed node always has a cidx-server systemd unit; on first boot the
server back-fills host/port/workers from its ExecStart (Bug #1232).  Every
e2e server therefore reads a STUB unit through ``SYSTEMD_UNIT_DIR`` -- never
this host's real unit (wrong bind values, Bug #1324) and never an empty dir
(the "absent from config.json and live ExecStart" WARNING).

This is the Python twin of ``write_stub_systemd_unit`` in
``e2e-automation.sh``: for the default host the bytes are identical, which a
unit test checks, so both round-trip through ``read_execstart_flags`` alike.
Servers a test starts itself pass their OWN host and port.
"""

from __future__ import annotations

from pathlib import Path

from tests.fixtures.real_server_home_guard import is_under_real_home

UNIT_FILE_NAME = "cidx-server.service"
DEFAULT_STUB_HOST = "127.0.0.1"


def write_stub_systemd_unit(
    unit_dir: Path, port: int, host: str = DEFAULT_STUB_HOST
) -> Path:
    """Write the stub unit into *unit_dir* (created); return the unit path.

    Raises ValueError, before creating anything, for a path inside the real
    server home.
    """
    if is_under_real_home(unit_dir):
        raise ValueError(
            f"refusing to write a stub unit in the real server home: {unit_dir}"
        )
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / UNIT_FILE_NAME
    unit.write_text(
        "[Unit]\n"
        "Description=CIDX e2e stub unit (never installed; read by the "
        "launch-key gap-fill)\n"
        "\n"
        "[Service]\n"
        "ExecStart=/usr/bin/python3 -m uvicorn code_indexer.server.app:app "
        f"--host {host} --port {port} --workers 1\n",
        encoding="utf-8",
    )
    return unit
