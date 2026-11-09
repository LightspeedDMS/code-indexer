"""The clone seam as the core resolves it: by mode, through entry points.

* ``local`` -- the module registering ``local`` on ``code_indexer.clone_backend``
  (cidx-backend-workspace), on the core runtime, passes the conformance suite;
* ``delegated`` -- the core's workspace-mcp clone-path client, resolved under
  ``delegated``, passes the same suite against the testkit's fake workspace-mcp
  (its HTTP calls answered by the fake service); so does the fake's own client
  when it is the installed delegate;
* ``ontap`` / ``cow-daemon`` resolve to their providers from their config
  sections (their services are remote: covered by the unit tests with
  doubles, not by this suite);
* nothing installed for a mode refuses, naming the seam; an unknown
  ``clone_backend`` refuses at startup instead of becoming ``local``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from cidx_sdk.errors import SeamUnavailableError
from cidx_sdk.seams.clone import CloneBackendSettings
from cidx_testkit.conformance.clone import (
    CloneBackendContract,
    CloneSubject,
    local_subject,
)
from cidx_testkit.fakes.workspace_mcp import (
    FakeWorkspaceMcp,
    FakeWorkspaceMcpCloneBackend,
    FakeWorkspaceMcpHttp,
    register_clone_delegate,
    unregister_clone_delegate,
)

from code_indexer.server.services import clone_backend_plugin
from code_indexer.server.startup.clone_backend_wiring import build_snapshot_manager
from code_indexer.server.storage.shared.clone_backend import (
    CowDaemonBackend,
    DelegatedCloneBackend,
    LocalCloneBackend,
    OntapCloneBackend,
    core_clone_services,
)

_ENDPOINT = "http://workspace-mcp.example.com"


@pytest.fixture(autouse=True)
def _fresh_resolver(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient install: only what this tree declares, plus what a test adds."""
    monkeypatch.setattr(clone_backend_plugin, "entry_points", lambda **_: [])
    clone_backend_plugin.reset_cache()
    yield
    clone_backend_plugin.reset_cache()


def _settings(
    base: Path, *, ontap: Any = None, cow_daemon: Any = None, endpoint: Any = None
) -> CloneBackendSettings:
    return CloneBackendSettings(
        services=core_clone_services(),
        versioned_base=str(base),
        ontap=ontap,
        cow_daemon=cow_daemon,
        endpoint=endpoint,
    )


class TestLocalModeAsResolved(CloneBackendContract):
    @pytest.fixture
    def clone_subject(self, tmp_path: Path) -> CloneSubject:
        base = tmp_path / "golden"
        base.mkdir()
        backend = clone_backend_plugin.resolve_clone_backend("local", _settings(base))
        assert isinstance(backend, LocalCloneBackend)
        return local_subject(backend, base)


class TestDelegatedModeAgainstTheFakeService(CloneBackendContract):
    """The core's own delegated client, its HTTP answered by the fake."""

    @pytest.fixture
    def clone_subject(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> CloneSubject:
        service = FakeWorkspaceMcp(store=tmp_path / "store")
        monkeypatch.setitem(sys.modules, "requests", FakeWorkspaceMcpHttp(service))
        base = tmp_path / "golden"
        base.mkdir()
        endpoint = SimpleNamespace(endpoint=_ENDPOINT, api_key="example-key")
        backend = clone_backend_plugin.resolve_clone_backend(
            "delegated", _settings(base, endpoint=endpoint)
        )
        assert isinstance(backend, DelegatedCloneBackend)
        return local_subject(backend, base)


class TestFakeDelegateAsResolved(CloneBackendContract):
    """An installed delegate (here the fake) is found as ``delegated``."""

    @pytest.fixture
    def clone_subject(self, tmp_path: Path) -> Iterator[CloneSubject]:
        service = FakeWorkspaceMcp(store=tmp_path / "store")
        registration = register_clone_delegate(service)
        clone_backend_plugin.reset_cache()
        try:
            base = tmp_path / "golden"
            base.mkdir()
            backend = clone_backend_plugin.resolve_clone_backend(
                "delegated", _settings(base)
            )
            assert isinstance(backend, FakeWorkspaceMcpCloneBackend)
            yield local_subject(backend, base)
        finally:
            unregister_clone_delegate(registration)
            clone_backend_plugin.reset_cache()


def test_every_mode_has_exactly_one_in_tree_provider() -> None:
    for mode in ("local", "ontap", "cow-daemon", "delegated"):
        assert clone_backend_plugin.installed_clone_providers(mode) == [mode]


def test_ontap_and_cow_daemon_resolve_from_their_sections(tmp_path: Path) -> None:
    ontap = SimpleNamespace(
        endpoint="198.51.100.10",
        admin_user="example-admin",
        admin_password="example-password",
        svm_name="svm1",
        parent_volume="example_vol",
        mount_point="/mnt/example",
    )
    cow = SimpleNamespace(
        daemon_url="http://198.51.100.20:8081",
        api_key="example-key",
        mount_point="/mnt/example",
        poll_interval_seconds=1,
        timeout_seconds=30,
        request_timeout_seconds=10,
        daemon_storage_path="/srv/example",
    )
    resolve = clone_backend_plugin.resolve_clone_backend
    assert isinstance(
        resolve("ontap", _settings(tmp_path, ontap=ontap)), OntapCloneBackend
    )
    assert isinstance(
        resolve("cow-daemon", _settings(tmp_path, cow_daemon=cow)), CowDaemonBackend
    )
    with pytest.raises(ValueError):
        resolve("ontap", _settings(tmp_path))


def test_a_mode_with_nothing_installed_refuses_naming_the_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cidx_sdk.seams import resolver

    monkeypatch.setattr(resolver, "_extra_entry_points", {})
    clone_backend_plugin.reset_cache()
    with pytest.raises(SeamUnavailableError, match="'clone'"):
        clone_backend_plugin.resolve_clone_backend("local", _settings(tmp_path))


def test_an_unknown_clone_backend_refuses_instead_of_becoming_local(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(clone_backend="cow-deamon")
    with pytest.raises(RuntimeError, match="Unknown clone_backend 'cow-deamon'"):
        build_snapshot_manager(config, versioned_base=str(tmp_path))


def test_the_snapshot_manager_gets_the_resolved_local_backend(tmp_path: Path) -> None:
    manager = build_snapshot_manager(
        SimpleNamespace(clone_backend="local"), versioned_base=str(tmp_path)
    )
    assert isinstance(manager._clone_backend, LocalCloneBackend)
