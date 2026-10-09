"""Server queries use server-managed provider endpoints; repository
configuration cannot select them (temporal path).

`MultiSearchService._search_temporal_sync()` loads a repo's config via its
OWN direct `ConfigManager.load_verified_config(repo_path)` call -- a
SEPARATE seam from `search_service._load_repo_config()` used by the
semantic search paths -- and hands the result straight to
`execute_temporal_query_with_fusion(config=..., ...)`, which constructs the
per-commit embedding client from `config.voyage_ai` / `config.cohere`. A
repository-authored `.code-indexer/config.json` must not be able to choose
that endpoint here either.

This test drives the REAL `_search_temporal_sync()` method (mirroring the
established mocking pattern in
tests/unit/server/multi/test_temporal_cache_injection_1170.py):
`ConfigManager.load_verified_config` is patched to return a real `Config`
object (not a mock) carrying a repository-configured endpoint,
`FilesystemVectorStore` and `execute_temporal_query_with_fusion` are stood
in as collaborators, and the config object `execute_temporal_query_with_fusion`
actually receives is inspected.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from _pytest.monkeypatch import MonkeyPatch  # noqa: F401 (typing aid only)

# Stub optional heavy/absent dependencies pulled in by this import chain,
# mirroring tests/unit/server/multi/test_temporal_cache_injection_1170.py.
_STUB_MODULES = [
    "google",
    "google.protobuf",
    "google.protobuf.descriptor",
    "google.protobuf.descriptor_pb2",
    "google.protobuf.descriptor_pool",
    "google.protobuf.internal",
    "google.protobuf.internal.builder",
    "google.protobuf.message",
    "google.protobuf.reflection",
    "google.protobuf.symbol_database",
    "google.protobuf.runtime_version",
    "rich",
    "rich.console",
    "rich.markup",
    "rich.table",
    "rich.panel",
    "rich.progress",
    "rich.text",
    "rich.syntax",
    "rich.traceback",
    "rich.logging",
    "pathspec",
    "code_indexer.scip.protobuf.scip_pb2",
    "code_indexer.scip.protobuf",
    "numpy",
    "msgpack",
]
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = MagicMock()

from code_indexer.config import CohereConfig, Config, VoyageAIConfig  # noqa: E402
from code_indexer.server.multi.multi_search_service import (  # noqa: E402
    MultiSearchService,
)
from code_indexer.server.multi.multi_search_config import MultiSearchConfig  # noqa: E402
from code_indexer.server.multi.models import InternalMultiSearchRequest  # noqa: E402
from code_indexer.services.temporal.temporal_search_service import (  # noqa: E402
    TemporalSearchResults,
)

_REPO_CONFIGURED_VOYAGE_ENDPOINT = "http://127.0.0.1:9/voyage-embeddings"
_REPO_CONFIGURED_COHERE_ENDPOINT = "http://127.0.0.1:9/cohere-embed"


def _repo_configured_config() -> Config:
    """A real Config, not a mock -- as if loaded from a repository-authored
    config.json naming a repository-configured endpoint."""
    cfg = Config()
    cfg.voyage_ai.api_endpoint = _REPO_CONFIGURED_VOYAGE_ENDPOINT
    cfg.cohere.api_endpoint = _REPO_CONFIGURED_COHERE_ENDPOINT
    return cfg


def test_temporal_search_resets_provider_endpoints_before_fusion() -> None:
    captured_configs = []

    def _fake_fusion(*args, **kwargs):
        captured_configs.append(kwargs.get("config"))
        return TemporalSearchResults(
            results=[],
            query=str(kwargs.get("query_text", "")),
            filter_type="none",
            filter_value=None,
            total_found=0,
        )

    request = InternalMultiSearchRequest(
        query="test query",
        search_type="temporal",
        repositories=["repo1"],
        limit=5,
        min_score=None,
        language=None,
        path_filter=None,
        exclude_language=None,
        exclude_path=None,
        accuracy=None,
        file_extensions=None,
        no_embedding_cache_shortcut=False,
        temporal_embedder=None,
        precomputed_query_vector=None,
        precomputed_query_vector_digest=None,
    )

    import code_indexer.services.temporal.temporal_fusion_dispatch as _tfd
    import code_indexer.config as _cfg_mod
    import code_indexer.storage.filesystem_vector_store as _fvs_mod

    svc = MultiSearchService(MultiSearchConfig(max_workers=1))

    with (
        patch.object(
            _tfd, "execute_temporal_query_with_fusion", side_effect=_fake_fusion
        ),
        # Shaped as .../golden-repos/<alias>/ so resolve_golden_repo_coordinates
        # resolves it structurally, without needing real app.state.golden_repos_dir.
        patch.object(
            svc, "_get_repository_path", return_value="/tmp/golden-repos/repo1"
        ),
        patch.object(
            _cfg_mod.ConfigManager,
            "load_verified_config",
            return_value=_repo_configured_config(),
        ),
        patch.object(_fvs_mod, "FilesystemVectorStore", return_value=MagicMock()),
    ):
        svc._search_temporal_sync("repo1", request)

    svc.thread_executor.shutdown(wait=False)

    assert captured_configs, "execute_temporal_query_with_fusion was never called"
    used_config = captured_configs[0]
    assert used_config is not None, "execute_temporal_query_with_fusion got config=None"
    assert used_config.voyage_ai.api_endpoint == VoyageAIConfig().api_endpoint, (
        "SECURITY: the multi-search temporal path must reset "
        "voyage_ai.api_endpoint to the server-managed default before "
        f"constructing the per-commit embedder. Got "
        f"{used_config.voyage_ai.api_endpoint!r}"
    )
    assert used_config.cohere.api_endpoint == CohereConfig().api_endpoint, (
        "SECURITY: the multi-search temporal path must reset "
        "cohere.api_endpoint to the server-managed default. Got "
        f"{used_config.cohere.api_endpoint!r}"
    )
