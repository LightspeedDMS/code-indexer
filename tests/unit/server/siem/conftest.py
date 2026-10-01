"""Shared fixtures for SIEM delivery tests.

The Chronicle stand-in is the REAL SecOps sidecar process (loopback only);
no Chronicle client or HTTP layer is mocked.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.services.siem_delivery.destination import (
    Destination,
    resolve_destination,
)
from code_indexer.server.utils.siem_delivery_config import SiemDeliveryConfig
from tests.fixtures.secops_sidecar.harness import SidecarHandle, start_sidecar

from .backends import _pg_session_pool, pg_pool, siem_backend  # noqa: F401

_SCRATCH_ROOT = Path.home() / ".tmp" / "siem-delivery-unit"


def new_scratch_dir() -> Path:
    _SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=str(_SCRATCH_ROOT)))


@pytest.fixture()
def scratch_dir() -> Iterator[Path]:
    path = new_scratch_dir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def siem_sidecar_session() -> Iterator[SidecarHandle]:
    scratch = new_scratch_dir()
    handle = start_sidecar(scratch)
    try:
        yield handle
    finally:
        handle.stop()
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture()
def siem_sidecar(siem_sidecar_session: SidecarHandle) -> SidecarHandle:
    siem_sidecar_session.control.reset()
    return siem_sidecar_session


def harness_section(sidecar: SidecarHandle, **overrides: object) -> SiemDeliveryConfig:
    coords = sidecar.coords
    values = dict(
        enabled=True,
        harness_endpoint=coords.harness_endpoint,
        api_version=coords.api_version,
        project_id=coords.project,
        location=coords.location,
        instance_id=coords.instance,
        service_account_key_path=str(sidecar.key_material.key_file_path),
        source_instance_label="example-label",
    )
    values.update(overrides)
    return SiemDeliveryConfig(**values)  # type: ignore[arg-type]


def harness_destination(sidecar: SidecarHandle, **overrides: object) -> Destination:
    dest = resolve_destination(
        harness_section(sidecar, **overrides), harness_active=True
    )
    assert dest is not None
    return dest


@pytest.fixture()
def http_factory() -> HttpClientFactory:
    return HttpClientFactory(fault_injection_service=None)
