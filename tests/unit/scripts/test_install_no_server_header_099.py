"""Shipped cidx-server launch lines start uvicorn without the Server header."""

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_UNIT_SOURCES = [
    _ROOT / "scripts" / "install-cidx-server.sh",
    _ROOT / "deployment" / "cidx-server.service",
]


def _uvicorn_execstarts(path: Path) -> list:
    return [
        line
        for line in path.read_text().splitlines()
        if line.startswith("ExecStart=")
        and "-m uvicorn code_indexer.server.app" in line
    ]


@pytest.mark.parametrize("path", _UNIT_SOURCES, ids=lambda p: p.name)
def test_cidx_server_execstart_disables_server_header(path):
    lines = _uvicorn_execstarts(path)
    assert lines, f"no uvicorn ExecStart in {path}"
    for line in lines:
        assert line.split().count("--no-server-header") == 1, line
