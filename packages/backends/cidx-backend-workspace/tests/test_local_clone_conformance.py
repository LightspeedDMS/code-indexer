"""The local clone modes: ``local`` passes the clone seam's conformance suite;
``ontap`` and ``cow-daemon`` are built from their settings (their services are
remote, so their behaviour is covered by the core's unit tests with doubles)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cidx_backend_workspace.clone import (
    CowDaemonBackend,
    LocalCloneBackend,
    OntapCloneBackend,
    make_cow_daemon_clone_backend,
    make_local_clone_backend,
    make_ontap_clone_backend,
)
from cidx_sdk.seams.clone import CloneBackendSettings
from cidx_testkit.conformance.clone import (
    CloneBackendContract,
    CloneSubject,
    local_subject,
    plain_clone_services,
)


def _settings(
    *, versioned_base: str = "", ontap: Any = None, cow_daemon: Any = None
) -> CloneBackendSettings:
    return CloneBackendSettings(
        services=plain_clone_services(),
        versioned_base=versioned_base,
        ontap=ontap,
        cow_daemon=cow_daemon,
    )


class TestLocalCloneBackend(CloneBackendContract):
    @pytest.fixture
    def clone_subject(self, tmp_path: Path) -> CloneSubject:
        base = tmp_path / "golden"
        base.mkdir()
        backend = make_local_clone_backend(_settings(versioned_base=str(base)))
        return local_subject(backend, base)


def test_the_local_factory_builds_a_local_backend(tmp_path: Path) -> None:
    backend = make_local_clone_backend(_settings(versioned_base=str(tmp_path)))
    assert isinstance(backend, LocalCloneBackend)


def test_the_ontap_factory_needs_its_section_and_builds_a_flexclone_backend() -> None:
    with pytest.raises(ValueError):
        make_ontap_clone_backend(_settings())
    ontap = SimpleNamespace(
        endpoint="198.51.100.10",
        admin_user="example-admin",
        admin_password="example-password",
        svm_name="svm1",
        parent_volume="example_vol",
        mount_point="/mnt/example",
    )
    assert isinstance(
        make_ontap_clone_backend(_settings(ontap=ontap)), OntapCloneBackend
    )


def test_the_cow_daemon_factory_needs_its_section_and_builds_a_rest_client() -> None:
    with pytest.raises(ValueError):
        make_cow_daemon_clone_backend(_settings())
    cow = SimpleNamespace(
        daemon_url="http://198.51.100.20:8081",
        api_key="example-key",
        mount_point="/mnt/example",
        poll_interval_seconds=1,
        timeout_seconds=30,
        request_timeout_seconds=10,
        daemon_storage_path="/srv/example",
    )
    backend = make_cow_daemon_clone_backend(_settings(cow_daemon=cow))
    assert isinstance(backend, CowDaemonBackend)
