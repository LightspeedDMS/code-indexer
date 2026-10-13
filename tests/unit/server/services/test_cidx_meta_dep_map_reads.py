"""Dependency-map READS on a mount-less pod go through cidx-meta's shared content.

Under a delegated content backend the readers (depmap MCP parser, domain and
dashboard services, health detector, quick-reference guide) walk the synced
read mirror of the shared content; under local they read cidx-meta exactly as
before. Parity: the same parser over the local tree and over the mirror gives
the same answers, and the pod's own cidx-meta is never touched.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from code_indexer.server.services import cidx_meta_read_mirror as crm
from code_indexer.server.services.cidx_meta_content import (
    CidxMetaUnavailableError,
    shared_cidx_meta,
)
from tests.unit.server.cidx_meta_seam_fakes import MetaWorld


@pytest.fixture
def meta_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetaWorld:
    crm.reset_mirrors()
    yield MetaWorld(tmp_path, monkeypatch)
    crm.reset_mirrors()


DOMAINS = [
    {"name": "billing", "description": "d", "participating_repos": ["pay", "ledger"]},
    {"name": "identity", "description": "d", "participating_repos": ["auth"]},
]


def _domain_md(name: str, repos: List[str], incoming: List[str]) -> str:
    repo_lines = "\n".join(f"  - {r}" for r in repos)
    rows = "".join(
        f"| {r} | {repos[0]} | identity | Code-level | why | ev |\n" for r in incoming
    )
    return (
        f"---\nname: {name}\nparticipating_repos:\n{repo_lines}\n---\n"
        f"# Domain Analysis: {name}\n\n## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n" + rows
    )


def _seed_map(world: MetaWorld) -> None:
    world.seed("dependency-map/_domains.json", json.dumps(DOMAINS))
    world.seed("dependency-map/_index.md", "# Index\n")
    world.seed(
        "dependency-map/billing.md", _domain_md("billing", ["pay", "ledger"], ["auth"])
    )
    world.seed("dependency-map/identity.md", _domain_md("identity", ["auth"], []))
    world.seed("pay.md", "---\nname: pay\n---\nPayments service.\n")
    world.seed("memories/m.md", "---\na: 1\n---\n")
    world.seed("xray-patterns/__any__/p.yaml", "name: p\n")
    world.seed("dependency-map.staging/x.md", "staging\n")


def _mirror(world: MetaWorld) -> Path:
    world.delegated()
    root = crm.synced_read_root(world.pod_meta)
    assert root is not None
    return root


# ── the mirror ───────────────────────────────────────────────────────────


def test_local_backend_has_no_mirror(meta_world: MetaWorld) -> None:
    assert crm.synced_read_root(meta_world.local_meta) is None


def test_mirror_holds_exactly_the_read_set(meta_world: MetaWorld) -> None:
    _seed_map(meta_world)
    pod_before = meta_world.pod_tree()
    meta_world.delegated()
    with meta_world.forbid_local_io():
        root = crm.synced_read_root(meta_world.pod_meta)
    assert root == meta_world.pod_golden / crm.MIRROR_DIRNAME
    mirrored = {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p.name != ".mirror-state.json"
    }
    expected = {
        rel: data for rel, data in meta_world.remote_tree().items() if crm.mirrored(rel)
    }
    assert mirrored == expected
    assert "memories/m.md" not in mirrored
    assert "dependency-map.staging/x.md" not in mirrored
    meta_world.local_mode()
    assert meta_world.pod_tree() == pod_before


def test_mirror_fetches_only_changes_and_drops_deletions(
    meta_world: MetaWorld,
) -> None:
    _seed_map(meta_world)
    root = _mirror(meta_world)
    meta_world.backend.fetch_calls.clear()
    mirror = crm.cidx_meta_read_mirror(meta_world.pod_meta)
    assert mirror is not None

    (meta_world.remote_meta / "dependency-map" / "_index.md").write_text("# New\n")
    (meta_world.remote_meta / "dependency-map" / "identity.md").unlink()
    mirror.sync(force=True)
    assert meta_world.backend.fetch_calls == [["dependency-map/_index.md"]]
    assert (root / "dependency-map" / "_index.md").read_text() == "# New\n"
    assert not (root / "dependency-map" / "identity.md").exists()

    meta_world.backend.fetch_calls.clear()
    mirror.sync(force=True)
    assert meta_world.backend.fetch_calls == []  # nothing changed


def test_mirror_sync_is_rate_limited(meta_world: MetaWorld) -> None:
    _seed_map(meta_world)
    _mirror(meta_world)
    calls = meta_world.backend.manifest_calls
    crm.synced_read_root(meta_world.pod_meta)
    assert meta_world.backend.manifest_calls == calls


@pytest.mark.parametrize("failure", ["manifest", "fetch", "record", "missing"])
def test_mirror_failures_raise_never_empty(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from code_indexer.server.storage.shared.content_backend import FetchedFile

    _seed_map(meta_world)
    meta_world.delegated()
    backend = meta_world.backend
    if failure == "manifest":
        backend.fail = ConnectionError("down")
    elif failure == "fetch":
        monkeypatch.setattr(
            backend, "fetch", lambda *a: (_ for _ in ()).throw(ConnectionError("x"))
        )
    elif failure == "record":
        monkeypatch.setattr(
            backend,
            "fetch",
            lambda root, paths: iter(
                [
                    FetchedFile(path=p, blob_sha=None, data=b"", error="EIO")
                    for p in paths
                ]
            ),
        )
    else:
        monkeypatch.setattr(backend, "fetch", lambda root, paths: iter([]))
    with pytest.raises(CidxMetaUnavailableError):
        crm.synced_read_root(meta_world.pod_meta)


# ── readers over the mirror ──────────────────────────────────────────────


def _parser_answers(root: Path) -> Dict[str, Any]:
    from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser

    parser = DepMapMCPParser(root)
    return {
        "consumers": parser.find_consumers("pay"),
        "repo_domains": parser.get_repo_domains("pay"),
        "summary": parser.get_domain_summary("billing"),
        "graph": parser.get_cross_domain_graph(),
    }


def test_depmap_parser_parity(meta_world: MetaWorld) -> None:
    _seed_map(meta_world)
    local = _parser_answers(meta_world.local_meta)
    root = _mirror(meta_world)
    with meta_world.forbid_local_io():
        delegated = _parser_answers(root)
    assert delegated == local
    assert local["repo_domains"]  # the fixture is not trivially empty


def _without_dir(report: Any) -> Dict[str, Any]:
    return {k: v for k, v in report.to_dict().items() if k != "output_dir"}


def test_health_detector_parity(meta_world: MetaWorld) -> None:
    from code_indexer.server.services.dep_map_health_detector import (
        DepMapHealthDetector,
    )

    _seed_map(meta_world)
    local = DepMapHealthDetector().detect(meta_world.local_meta / "dependency-map")
    root = _mirror(meta_world)
    with meta_world.forbid_local_io():
        delegated = DepMapHealthDetector().detect(root / "dependency-map")
    assert _without_dir(delegated) == _without_dir(local)
    assert local.to_dict()["anomalies"] is not None


def _service(golden: Path) -> Any:
    from code_indexer.server.services.dependency_map_service import (
        DependencyMapService,
    )

    service = object.__new__(DependencyMapService)
    service._golden_repos_manager = SimpleNamespace(
        golden_repos_dir=str(golden),
        get_actual_repo_path=lambda alias: str(golden / alias),
    )
    return service


def test_service_read_path_is_the_mirror_only_when_delegated(
    meta_world: MetaWorld,
) -> None:
    _seed_map(meta_world)
    assert _service(meta_world.local_golden).cidx_meta_read_path == (
        meta_world.local_meta
    )
    meta_world.delegated()
    with meta_world.forbid_local_io():
        root = _service(meta_world.pod_golden).cidx_meta_read_path
    assert root == meta_world.pod_golden / crm.MIRROR_DIRNAME
    assert (root / "dependency-map" / "_domains.json").exists()


def test_service_read_path_fails_loud(meta_world: MetaWorld) -> None:
    meta_world.delegated()
    meta_world.backend.fail = ConnectionError("down")
    with pytest.raises(CidxMetaUnavailableError):
        _service(meta_world.pod_golden).cidx_meta_read_path


def test_guide_section_reads_the_mirror(
    meta_world: MetaWorld, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.server.mcp.handlers.guides import (
        _build_dependency_map_section,
        _dependency_map_read_root,
    )

    _seed_map(meta_world)
    local = _build_dependency_map_section(
        _dependency_map_read_root(meta_world.local_meta)
    )
    meta_world.delegated()
    with meta_world.forbid_local_io():
        delegated = _build_dependency_map_section(
            _dependency_map_read_root(meta_world.pod_meta)
        )
    assert local == delegated and "2 domains" in local

    crm.reset_mirrors()
    meta_world.backend.fail = ConnectionError("down")
    with caplog.at_level(logging.ERROR):
        section = _build_dependency_map_section(
            _dependency_map_read_root(meta_world.pod_meta)
        )
    assert section == ""
    assert any("dependency map unavailable" in r.message for r in caplog.records)


def test_route_output_dir_is_the_mirror_and_cache_is_shared(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.storage.filesystem_backends import (
        FilesystemDashboardCacheBackend,
        SharedContentDashboardCacheBackend,
    )
    from code_indexer.server.web import dependency_map_routes as routes

    _seed_map(meta_world)
    meta_world.delegated()
    service = _service(meta_world.pod_golden)
    service.get_sentinel_dir = lambda: meta_world.pod_meta / "dependency-map"
    monkeypatch.setattr(routes, "_get_dep_map_service_from_state", lambda: service)
    with meta_world.forbid_local_io():
        out = routes._get_dep_map_output_dir()
        cache = routes._get_dashboard_cache_backend()
    assert out == meta_world.pod_golden / crm.MIRROR_DIRNAME / "dependency-map"
    assert isinstance(cache, SharedContentDashboardCacheBackend)

    meta_world.local_mode()
    local_service = _service(meta_world.local_golden)
    local_service.get_sentinel_dir = lambda: meta_world.local_meta / "dependency-map"
    monkeypatch.setattr(
        routes, "_get_dep_map_service_from_state", lambda: local_service
    )
    assert routes._get_dep_map_output_dir() == meta_world.local_meta / "dependency-map"
    cache = routes._get_dashboard_cache_backend()
    assert type(cache) is FilesystemDashboardCacheBackend


def test_shared_cidx_meta_is_the_mirror_source(meta_world: MetaWorld) -> None:
    meta_world.delegated()
    shared = shared_cidx_meta(meta_world.pod_meta)
    mirror = crm.cidx_meta_read_mirror(meta_world.pod_meta)
    assert shared is not None and mirror is not None
    assert mirror._shared.root == shared.root


# ── read-your-writes on the writing pod ──────────────────────────────────


def test_own_write_is_read_immediately(meta_world: MetaWorld) -> None:
    """A write through the seam on this pod is in the mirror before the next
    read -- no sync, no manifest call, inside the TTL."""
    _seed_map(meta_world)
    root = _mirror(meta_world)
    shared = shared_cidx_meta(meta_world.pod_meta)
    assert shared is not None
    calls = meta_world.backend.manifest_calls
    with meta_world.forbid_local_io():
        shared.put_text("dependency-map/_index.md", "# Rewritten\n")
        shared.put_text("dependency-map/new-domain.md", "# New\n")
        shared.put_text("memories/not-mirrored.md", "x")
        again = crm.synced_read_root(meta_world.pod_meta)
    assert again == root
    assert meta_world.backend.manifest_calls == calls  # served from the mirror
    assert (root / "dependency-map" / "_index.md").read_text() == "# Rewritten\n"
    assert (root / "dependency-map" / "new-domain.md").read_text() == "# New\n"
    assert not (root / "memories").exists()
    # And the mirror's state agrees: a forced sync fetches nothing.
    meta_world.backend.fetch_calls.clear()
    crm.cidx_meta_read_mirror(meta_world.pod_meta).sync(force=True)
    assert meta_world.backend.fetch_calls == []


def test_own_delete_is_gone_immediately(meta_world: MetaWorld) -> None:
    _seed_map(meta_world)
    root = _mirror(meta_world)
    shared = shared_cidx_meta(meta_world.pod_meta)
    assert shared is not None
    with meta_world.forbid_local_io():
        shared.delete("dependency-map/identity.md")
        shared.rename("pay.md", "payments.md", overwrite=False)
        crm.synced_read_root(meta_world.pod_meta)
    assert not (root / "dependency-map" / "identity.md").exists()
    assert not (root / "pay.md").exists()
    assert (root / "payments.md").read_text().endswith("Payments service.\n")


def test_other_pods_writes_appear_within_the_ttl(meta_world: MetaWorld) -> None:
    """Cross-pod staleness is bounded by SYNC_TTL_SECONDS (documented)."""
    _seed_map(meta_world)
    root = _mirror(meta_world)
    (meta_world.remote_meta / "dependency-map" / "_index.md").write_text(
        "# Other pod\n"
    )
    crm.synced_read_root(meta_world.pod_meta)
    assert (root / "dependency-map" / "_index.md").read_text() == "# Index\n"
    mirror = crm.cidx_meta_read_mirror(meta_world.pod_meta)
    mirror._synced_at -= crm.SYNC_TTL_SECONDS  # the TTL has passed
    crm.synced_read_root(meta_world.pod_meta)
    assert (root / "dependency-map" / "_index.md").read_text() == "# Other pod\n"


def test_failed_write_through_forces_a_resync(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_map(meta_world)
    root = _mirror(meta_world)
    shared = shared_cidx_meta(meta_world.pod_meta)
    assert shared is not None
    real_write = crm._atomic_write

    def failing(target, data):
        raise OSError("disk full")

    monkeypatch.setattr(crm, "_atomic_write", failing)
    shared.put_text("dependency-map/_index.md", "# Rewritten\n")  # still succeeds
    monkeypatch.setattr(crm, "_atomic_write", real_write)
    calls = meta_world.backend.manifest_calls
    crm.synced_read_root(meta_world.pod_meta)
    assert meta_world.backend.manifest_calls == calls + 1  # re-synced at once
    assert (root / "dependency-map" / "_index.md").read_text() == "# Rewritten\n"
