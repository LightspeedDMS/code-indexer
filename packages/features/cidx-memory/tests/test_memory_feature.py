"""The cidx-memory Feature object, as the SDK contract sees it."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Sequence, cast
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from cidx_memory import __version__
from cidx_memory.config import (
    MEMORY_CONFIG_SECTION,
    MemoryRetrievalConfig,
    memory_retrieval_config_from,
)
from cidx_memory.feature import (
    NUDGE_FILE,
    REQUIRED_CAPABILITIES,
    MemoryFeature,
    make_feature,
)
from cidx_memory.server import lifecycle, retrieval, routes, runtime
from cidx_memory.server.memory_candidate_retriever import MemoryCandidate
from cidx_sdk import (
    FeatureContext,
    ROUTER_AUTH_API,
    Feature,
    SearchAugmentRequest,
    sdk_satisfies,
)
from cidx_sdk.capabilities import ACCESS_PROJECTION, CONFIG
from cidx_sdk.feature import HEALTHY

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


@pytest.fixture
def no_context_left() -> Iterator[None]:
    """Leave no feature context or store behind."""
    yield
    runtime.STORE.uninstall()
    runtime.CONTEXT.uninstall()


class _ContextImpl:
    """A started feature's context holding the given capabilities."""

    def __init__(self, capabilities: Dict[str, object]) -> None:
        self._capabilities = capabilities

    def has_capability(self, name: str) -> bool:
        return name in self._capabilities

    def capability(self, name: str) -> object:
        return self._capabilities[name]


def _Context(capabilities: Dict[str, object]) -> FeatureContext:
    """A started feature's context holding *capabilities*."""
    return cast(FeatureContext, _ContextImpl(capabilities))


class _Section:
    def __init__(self, values: Dict[str, object]) -> None:
        self._values = values

    def runtime_config(self) -> object:
        return None

    def section(self) -> Dict[str, object]:
        return dict(self._values)

    def subscribe(self, callback: Any) -> Any:
        return lambda: None


class _Projection:
    """An access projection that lets *visible* through and records calls."""

    def __init__(self, visible: Sequence[str]) -> None:
        self._visible = set(visible)
        self.calls: List[Any] = []

    def visible_memory_ids(self, viewer: str, memory_ids: Sequence[str]) -> List[str]:
        self.calls.append((viewer, list(memory_ids)))
        return [m for m in memory_ids if m in self._visible]


def test_factory_returns_a_feature_named_memory() -> None:
    feature = make_feature()
    assert isinstance(feature, Feature)
    assert isinstance(feature, MemoryFeature)
    assert feature.manifest.name == "memory"
    assert feature.manifest.version == __version__
    assert sdk_satisfies(feature.manifest.requires_sdk)


def test_requires_config_and_the_access_projection() -> None:
    required = make_feature().manifest.required_capabilities
    assert required == REQUIRED_CAPABILITIES
    assert set(required) == {CONFIG, ACCESS_PROJECTION}


def test_entry_point_is_declared_under_the_feature_name() -> None:
    text = _PYPROJECT.read_text(encoding="utf-8")
    group = re.search(
        r'^\[project\.entry-points\."code_indexer\.features"\]\n(.*?)(?:\n\[|\Z)',
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert group is not None, "no code_indexer.features entry-point group"
    assert 'memory = "cidx_memory.feature:make_feature"' in group.group(1)


def test_tools_ship_their_docs_in_the_wheel() -> None:
    tools = make_feature().tools()
    assert [tool.name for tool in tools] == [
        "create_memory",
        "delete_memory",
        "edit_memory",
    ]
    for tool in tools:
        assert tool.doc_markdown.startswith(f"---\nname: {tool.name}\n")


def test_the_router_is_the_memories_api_behind_core_authentication() -> None:
    (contribution,) = make_feature().routers()
    assert contribution.prefix == "/api/v1/memories"
    assert contribution.auth == ROUTER_AUTH_API
    paths = {
        (route.path, tuple(sorted(route.methods)))
        for route in routes.router.routes
        if isinstance(route, APIRoute)
    }
    assert paths == {
        ("", ("POST",)),
        ("/{memory_id}", ("PUT",)),
        ("/{memory_id}", ("DELETE",)),
    }


def test_a_route_outside_the_core_authentication_answers_401() -> None:
    app = FastAPI()
    app.include_router(routes.router, prefix=routes.ROUTER_PREFIX)
    response = TestClient(app).post("/api/v1/memories", json={})
    assert response.status_code == 401


def test_the_search_section_is_relevant_memories_for_semantic_modes() -> None:
    (augmenter,) = make_feature().search_augmenters()
    assert augmenter.key == "relevant_memories"
    assert augmenter.search_modes == ("semantic", "hybrid")
    assert augmenter.wants_query_vector is True


def test_config_section_is_the_former_memory_retrieval_config() -> None:
    keys = {key.name: key for key in MEMORY_CONFIG_SECTION.keys}
    defaults = MemoryRetrievalConfig()
    assert set(keys) == set(vars(defaults))
    assert all(not key.bootstrap for key in keys.values())
    assert keys["memory_retrieval_enabled"].default is True
    assert keys["memory_retrieval_k_multiplier"].minimum == 1


def test_config_section_is_titled_and_labelled_for_the_config_screen() -> None:
    assert MEMORY_CONFIG_SECTION.title == "Memory Retrieval"
    assert all(key.label for key in MEMORY_CONFIG_SECTION.keys)


def test_memory_retrieval_config_from_ignores_unknown_keys() -> None:
    config = memory_retrieval_config_from({"memory_voyage_min_score": 0.9, "x": 1})
    assert config.memory_voyage_min_score == 0.9


def test_settings_are_read_from_the_config_capability(no_context_left: None) -> None:
    assert runtime.memory_retrieval_config() == MemoryRetrievalConfig()
    runtime.CONTEXT.install(
        _Context({CONFIG: _Section({"memory_retrieval_enabled": False})})
    )
    assert runtime.memory_retrieval_config().memory_retrieval_enabled is False
    assert retrieval.is_active() is False


def test_visibility_is_the_access_projection(no_context_left: None) -> None:
    candidates = [
        MemoryCandidate(memory_id="a" * 32, hnsw_score=0.9),
        MemoryCandidate(memory_id="b" * 32, hnsw_score=0.8),
    ]
    assert retrieval.visible_candidates(candidates, "example") == []
    projection = _Projection(["b" * 32])
    runtime.CONTEXT.install(_Context({ACCESS_PROJECTION: projection}))
    visible = retrieval.visible_candidates(candidates, "example")
    assert [c.memory_id for c in visible] == ["b" * 32]
    assert projection.calls == [("example", ["a" * 32, "b" * 32])]


def test_no_section_outside_semantic_modes_or_without_a_vector(
    no_context_left: None,
) -> None:
    fts = SearchAugmentRequest(
        username="u", query_text="q", search_mode="fts", limit=5, query_vector=[0.1]
    )
    no_vector = SearchAugmentRequest(
        username="u", query_text="q", search_mode="semantic", limit=5
    )
    assert retrieval.relevant_memories(fts) is None
    assert retrieval.relevant_memories(no_vector) is None


def test_startup_without_catalog_wiring_runs_no_store(no_context_left: None) -> None:
    host = SimpleNamespace(
        app_attr=lambda name: SimpleNamespace(state=SimpleNamespace()),
        refresh_scheduler=lambda: None,
    )
    with patch.object(lifecycle, "server_platform", return_value=host):
        started = lifecycle.MemoryRuntime()
        started.start(_Context({}))
    assert runtime.memory_store() is None
    started.stop()
    assert runtime.CONTEXT.get() is None


def test_startup_builds_the_store_over_the_catalog(
    no_context_left: None, tmp_path: Path
) -> None:
    golden = tmp_path / "data" / "golden-repos"
    state = SimpleNamespace(
        golden_repos_dir=str(golden),
        server_data_dir=str(tmp_path),
        cidx_meta_debouncer=object(),
    )
    host = SimpleNamespace(
        app_attr=lambda name: SimpleNamespace(state=state),
        refresh_scheduler=lambda: object(),
    )
    with (
        patch.object(lifecycle, "server_platform", return_value=host),
        patch(
            "cidx_memory.server.memory_store_service_factory.content_seam_delegated",
            return_value=False,
        ),
    ):
        started = lifecycle.MemoryRuntime()
        started.start(_Context({}))
    try:
        assert runtime.memory_store() is not None
        assert (golden / "cidx-meta" / "memories").is_dir()
        assert (tmp_path / "locks").is_dir()
    finally:
        started.stop()
    assert runtime.memory_store() is None


def test_health_reports_the_packaged_nudge() -> None:
    assert NUDGE_FILE.is_file()
    (report,) = make_feature().health()
    assert report.component == "memory-assets"
    assert report.status == HEALTHY
