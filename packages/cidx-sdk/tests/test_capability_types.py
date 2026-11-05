"""Capability Protocols, the slot type and the web request-state contract."""

from __future__ import annotations

import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest

import cidx_sdk
from cidx_sdk import (
    CAPABILITY_PROTOCOLS,
    KNOWN_CAPABILITIES,
    PRINCIPAL_STATE_ATTR,
    RENDER_STATE_ATTR,
    CapabilitySlot,
    ContentSeamCapability,
    Principal,
    RegexTargetCapability,
    RouterContribution,
    SnapshotPredicateCapability,
    TelemetryCapability,
)
from cidx_sdk.errors import InvalidManifestError


def test_every_known_capability_has_a_protocol() -> None:
    assert set(CAPABILITY_PROTOCOLS) == set(KNOWN_CAPABILITIES)


def test_sdk_minor_bumped_for_the_capability_types() -> None:
    # 0.2 added the capability types; later minors (0.3: the seams) keep them.
    assert cidx_sdk.sdk_satisfies(">=0.2")


class _Telemetry:
    def application_metrics(self) -> None:
        return None

    def application_meter(self) -> None:
        return None

    def span(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> Any:
        return nullcontext()


def test_protocols_are_runtime_checkable_structurally() -> None:
    assert isinstance(_Telemetry(), TelemetryCapability)
    assert not isinstance(object(), TelemetryCapability)


class _Seam:
    def is_delegated(self) -> bool:
        return True

    def source_for(self, repo_path: str, *, consumer: str) -> Optional[str]:
        return repo_path

    def iter_manifest_entries(
        self, source: str, prune_dirs: Sequence[str], *, deadline: Optional[float]
    ) -> Iterator[int]:
        return iter([1, 2])


class _Regex:
    def regex_target(self, repo_path: Path) -> Tuple[Path, Optional[str]]:
        return repo_path, None


class _Snapshots:
    def is_versioned_snapshot(self, path: str) -> bool:
        return ".versioned" in path

    def is_immutable_versioned_snapshot(self, path: str) -> bool:
        return False


def test_seam_shaped_capabilities_are_generic_over_seam_types() -> None:
    seam: ContentSeamCapability[str, int] = _Seam()
    assert list(seam.iter_manifest_entries("r", (), deadline=None)) == [1, 2]
    regex: RegexTargetCapability[str] = _Regex()
    assert regex.regex_target(Path("/r")) == (Path("/r"), None)
    assert isinstance(_Snapshots(), SnapshotPredicateCapability)


def test_slot_installs_replaces_and_empties() -> None:
    slot: CapabilitySlot[str] = CapabilitySlot("example")
    assert slot.get() is None
    slot.install("a")
    slot.install("b")
    assert slot.get() == "b"
    slot.uninstall()
    assert slot.get() is None


def test_slot_install_is_thread_safe() -> None:
    slot: CapabilitySlot[int] = CapabilitySlot("example")
    workers: List[threading.Thread] = [
        threading.Thread(target=slot.install, args=(n,)) for n in range(20)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert slot.get() in range(20)


def test_principal_and_state_attribute_names() -> None:
    principal = Principal(username="example-user", role="admin")
    assert (principal.username, principal.role) == ("example-user", "admin")
    assert PRINCIPAL_STATE_ATTR != RENDER_STATE_ATTR


def test_router_contribution_permission_and_prefix() -> None:
    router = RouterContribution(router=object(), prefix="/x", required_permission="p")
    assert router.required_permission == "p"
    with pytest.raises(InvalidManifestError):
        RouterContribution(router=object(), prefix="no-slash")


def test_router_contribution_nav_entry_needs_a_prefix_and_a_label() -> None:
    router = RouterContribution(
        router=object(), prefix="/admin/x", nav_label="X", nav_permission="p"
    )
    assert (router.nav_label, router.nav_permission) == ("X", "p")
    with pytest.raises(InvalidManifestError, match="prefix"):
        RouterContribution(router=object(), nav_label="X")
    with pytest.raises(InvalidManifestError, match="nav_label"):
        RouterContribution(router=object(), prefix="/admin/x", nav_permission="p")


def test_dependency_map_view_and_access_are_structural() -> None:
    from cidx_sdk import DependencyMapAccess, DependencyMapView

    class _View:
        def is_present(self) -> bool:
            return True

        def load_domains_json(self) -> list:
            return []

        def describe(self, entry: object) -> str:
            return ""

        def domain_names(self) -> list:
            return []

        def has_domain(self, name: str) -> bool:
            return False

        def read_text(self, name: str) -> str:
            raise FileNotFoundError(name)

        def label(self, name: str) -> str:
            return name

        def resolved_label(self, name: str) -> str:
            return name

        def within(self, name: str) -> bool:
            return False

    class _Access:
        restricted = True
        visible_repositories = frozenset({"repo"})
        view = _View()

    assert isinstance(_View(), DependencyMapView)
    assert isinstance(_Access(), DependencyMapAccess)
