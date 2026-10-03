"""Shared fixtures for SIEM delivery tests.

The Chronicle stand-in is the REAL SecOps sidecar process (loopback only);
no Chronicle client or HTTP layer is mocked.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.services.siem_delivery.credential import (
    SiemCredentialStore,
    StoredCredential,
)
from code_indexer.server.services.siem_delivery.db import SiemDb
from code_indexer.server.services.siem_delivery.destination import (
    Destination,
    resolve_destination,
)
from code_indexer.server.services.siem_delivery.sender import CredentialProvider
from code_indexer.server.services.token_encryption import derive_key_from_salt
from code_indexer.server.utils.siem_delivery_config import SiemDeliveryConfig
from tests.fixtures.secops_sidecar.harness import SidecarHandle, start_sidecar

from .backends import _pg_session_pool, pg_pool, siem_backend  # noqa: F401
from .web_ops_harness import ops  # noqa: F401  (the Web SIEM operator app)

# The real-~/.cidx-server guard and the server data-dir isolation for these
# tests live in tests/unit/server/conftest.py (Bug #1996).

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


# The server derives this key from .encryption_key_salt; tests use a salt.
TEST_ENCRYPTION_KEY = derive_key_from_salt("siem-unit-test-salt")


def sidecar_loader(
    sidecar: SidecarHandle, **overrides: Any
) -> Callable[[], StoredCredential]:
    """A credential loader serving the sidecar's key (as the store would)."""
    info = {**sidecar.read_key_file(), **overrides}
    return lambda: StoredCredential("test-credential", dict(info))


def seeded_store(db: SiemDb, sidecar: SidecarHandle) -> SiemCredentialStore:
    """The real encrypted store holding the sidecar's key."""
    store = SiemCredentialStore(db, TEST_ENCRYPTION_KEY)
    store.set(dict(sidecar.read_key_file()), actor="example-admin")
    return store


def sidecar_provider(
    http: HttpClientFactory, sidecar: SidecarHandle, token_timeout: float = 5.0
) -> CredentialProvider:
    return CredentialProvider(http, sidecar_loader(sidecar), token_timeout)
