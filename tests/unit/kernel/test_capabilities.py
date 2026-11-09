"""Kernel capability slots: no-host defaults and delegation to an installed host."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest

from code_indexer.kernel import capabilities as caps
from code_indexer.kernel.seam_types import (
    ContentEntry,
    ContentPage,
    ContentSeamSource,
    FetchedFile,
    XrayGraphRequest,
    XrayScanOutcome,
    XrayScanRequest,
)


class _FakeContentBackend:
    """A content backend that holds nothing."""

    is_local = False

    def manifest(self, repo_path: Any, **_: Any) -> ContentPage:
        return ContentPage(entries=(), next_cursor=None)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        return iter(())


class _FakeXrayBackend:
    """An X-Ray execution backend that executes nothing."""

    def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome:
        return XrayScanOutcome(results=[])

    def execute_graph_analysis(self, request: XrayGraphRequest) -> Dict[str, Any]:
        return {}


_ALL_SLOTS: Tuple[caps.CapabilitySlot[Any], ...] = (
    caps.CONTENT_SEAM_HOST,
    caps.REGEX_TARGET_HOST,
    caps.XRAY_PLACEMENT_HOST,
    caps.XRAY_CACHE_STORE_HOST,
    caps.RUNTIME_CONFIG_HOST,
    caps.TELEMETRY_HOST,
    caps.READ_SCOPE_HOST,
)


@pytest.fixture
def empty_slots() -> Iterator[None]:
    """Every slot empty for the test, restored afterwards."""
    saved = [slot.get() for slot in _ALL_SLOTS]
    for slot in _ALL_SLOTS:
        slot.uninstall()
    yield
    for slot, implementation in zip(_ALL_SLOTS, saved):
        if implementation is None:
            slot.uninstall()
        else:
            slot.install(implementation)


class TestCapabilitySlot:
    def test_install_get_uninstall(self) -> None:
        slot: caps.CapabilitySlot[str] = caps.CapabilitySlot("example")
        assert slot.get() is None
        slot.install("first")
        slot.install("second")
        assert slot.get() == "second"
        slot.uninstall()
        assert slot.get() is None


@pytest.mark.usefixtures("empty_slots")
class TestNoHostDefaults:
    """Without a host, each helper answers what the server answers outside a
    running server."""

    def test_content_seam_is_local(self) -> None:
        assert caps.content_seam_delegated() is False
        source = caps.content_seam_source("/repo", consumer="test")
        # The local mode's implementation reads the path itself.
        assert source.backend.is_local and source.root == "/repo"
        assert caps.pod_directory(source) == Path("/repo")

    def test_a_delegated_source_has_no_pod_directory(self) -> None:
        source = ContentSeamSource(backend=_FakeContentBackend(), root="/repo")
        assert caps.pod_directory(source) is None

    def test_manifest_without_host_raises(self) -> None:
        source = ContentSeamSource(backend=_FakeContentBackend(), root="/repo")
        with pytest.raises(RuntimeError):
            list(caps.iter_manifest_entries(source, ()))

    def test_regex_runs_locally_at_the_repo(self) -> None:
        assert caps.regex_target(Path("/repo")) == (Path("/repo"), None)

    def test_xray_executes_in_process(self) -> None:
        configured = _FakeXrayBackend()
        assert caps.configured_xray_execution_backend() is None
        assert (
            caps.xray_execution_backend_for(configured, "/repo", ["a.py"]) is configured
        )

    def test_no_config_no_telemetry_no_read_scope(self) -> None:
        def work() -> None:
            return None

        assert caps.runtime_config() is None
        assert caps.application_metrics() is None
        assert caps.application_meter() is None
        with caps.telemetry_span("cidx.test", {"k": "v"}):
            pass
        assert caps.carry_read_scope(work) is work


class _RecordingHost:
    """One object implementing every capability Protocol, recording calls."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Any]] = []
        self.source = ContentSeamSource(backend=_FakeContentBackend(), root="/root")

    def is_delegated(self) -> bool:
        return True

    def source_for(
        self, repo_path: str, *, consumer: str
    ) -> Optional[ContentSeamSource]:
        self.calls.append(("source_for", (repo_path, consumer)))
        return self.source

    def iter_manifest_entries(
        self,
        source: ContentSeamSource,
        prune_dirs: Sequence[str],
        *,
        deadline: Optional[float],
    ) -> Iterator[ContentEntry]:
        self.calls.append(("manifest", (tuple(prune_dirs), deadline)))
        return iter(
            [ContentEntry(path="a.py", size=1, blob_sha=None, is_symlink=False)]
        )

    def regex_target(self, repo_path: Path) -> Tuple[Path, Any]:
        return Path("/base"), "backend"

    def configured_backend(self) -> Any:
        return "configured"

    def backend_for(self, configured: Any, repo_path: Any, rel_paths: Any) -> Any:
        self.calls.append(("backend_for", (configured, repo_path, list(rel_paths))))
        return "chosen"

    def postgres_connection_pool(self, postgres_dsn: str) -> Any:
        self.calls.append(("cache_store", postgres_dsn))
        return "cache"

    def runtime_config(self) -> object:
        return {"storage_mode": "postgres"}

    def application_metrics(self) -> Any:
        return "metrics"

    def application_meter(self) -> Any:
        return "meter"

    def span(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> Any:
        self.calls.append(("span", (name, attributes)))
        from contextlib import nullcontext

        return nullcontext()

    def carry(self, fn: Callable[[], None]) -> Callable[[], None]:
        self.calls.append(("carry", fn))
        return fn


@pytest.mark.usefixtures("empty_slots")
def test_helpers_delegate_to_installed_hosts() -> None:
    host = _RecordingHost()
    for slot in _ALL_SLOTS:
        slot.install(host)

    def work() -> None:
        return None

    assert caps.content_seam_delegated() is True
    assert caps.content_seam_source("/repo", consumer="X") is host.source
    entries = list(caps.iter_manifest_entries(host.source, (".git",), deadline=5.0))
    assert [e.path for e in entries] == ["a.py"]
    assert caps.regex_target(Path("/repo")) == (Path("/base"), "backend")
    assert caps.configured_xray_execution_backend() == "configured"
    assert caps.xray_execution_backend_for(None, "/repo", ["a.py"]) == "chosen"
    store = caps.XRAY_CACHE_STORE_HOST.get()
    assert store is not None
    assert store.postgres_connection_pool("dsn") == "cache"
    assert caps.runtime_config() == {"storage_mode": "postgres"}
    assert caps.application_metrics() == "metrics"
    assert caps.application_meter() == "meter"
    with caps.telemetry_span("cidx.test", {"k": "v"}):
        pass
    assert caps.carry_read_scope(work) is work
    assert ("source_for", ("/repo", "X")) in host.calls
    assert ("manifest", ((".git",), 5.0)) in host.calls
    assert ("backend_for", (None, "/repo", ["a.py"])) in host.calls
    assert ("span", ("cidx.test", {"k": "v"})) in host.calls
    assert ("carry", work) in host.calls
