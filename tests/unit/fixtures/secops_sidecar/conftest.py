"""Fixtures for the mock SecOps receiver sidecar fidelity self-tests.

Every fixture drives the REAL sidecar subprocess over HTTP (no in-process
double).  Scratch files (the per-run generated key material, the sidecar log)
live under ``~/.tmp`` and are removed at teardown.

Fixtures:
  sidecar_scratch_dir     -- function-scoped scratch dir under ~/.tmp
  secops_sidecar          -- function-scoped sidecar (own process, own ports)
  secops_sidecar_session  -- one sidecar shared by the module's tests
  sidecar                 -- the session sidecar, reset before each test
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from tests.fixtures.secops_sidecar.harness import (
    SidecarHandle,
    live_handles,
    start_sidecar,
)

_SCRATCH_ROOT = Path.home() / ".tmp" / "secops-sidecar-selftests"


def _new_scratch_dir() -> Path:
    _SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=str(_SCRATCH_ROOT)))


@pytest.fixture()
def sidecar_scratch_dir() -> Iterator[Path]:
    path = _new_scratch_dir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def secops_sidecar(sidecar_scratch_dir: Path) -> Iterator[SidecarHandle]:
    handle = start_sidecar(sidecar_scratch_dir)
    try:
        yield handle
    finally:
        handle.stop()


@pytest.fixture(scope="session")
def secops_sidecar_session() -> Iterator[SidecarHandle]:
    scratch = _new_scratch_dir()
    handle = start_sidecar(scratch)
    try:
        yield handle
    finally:
        handle.stop()
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture()
def sidecar(secops_sidecar_session: SidecarHandle) -> SidecarHandle:
    """The shared sidecar, reset to an empty state before the test runs."""
    secops_sidecar_session.control.reset()
    return secops_sidecar_session


@pytest.fixture(scope="session", autouse=True)
def _no_leaked_sidecar_processes() -> Iterator[None]:
    """Fail the session if any harness-started sidecar is still running."""
    yield
    leaked = [h.process.pid for h in live_handles() if h.process.poll() is None]
    assert not leaked, f"sidecar processes leaked by the self-tests: {leaked}"
