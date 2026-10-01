"""Server queries use server-managed provider endpoints; repository
configuration cannot select them (reconstruct_temporal_backend path).

`reconstruct_temporal_backend()` loads a repo's config via its own direct
`ConfigManager.load_verified_config(repo_path)` call and returns it for the
caller (`semantic_query_manager.py`'s inline temporal path, and
`server/services/temporal_worker.py`'s standalone worker) to hand straight
to `execute_temporal_query_with_fusion(config=..., ...)`, which constructs
the per-commit embedding client from `config.voyage_ai` / `config.cohere`.
A repository-authored `.code-indexer/config.json` must not be able to
choose that endpoint here either.

This test drives the REAL `reconstruct_temporal_backend()` against a real
on-disk config.json (mirroring
test_reconstruct_temporal_backend_config_verification_1690.py); only
`BackendFactory.create` is mocked (an unrelated collaborator this function
also touches).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import CohereConfig, VoyageAIConfig
from code_indexer.server.query.semantic_query_manager import (
    reconstruct_temporal_backend,
)

_REPO_CONFIGURED_VOYAGE_ENDPOINT = "http://127.0.0.1:9/voyage-embeddings"
_REPO_CONFIGURED_COHERE_ENDPOINT = "http://127.0.0.1:9/cohere-embed"


@pytest.fixture
def isolated_tmp_root():
    """Rooted under `~/.tmp` (never bare `/tmp` -- project convention),
    immune to any real `.code-indexer/config.json` ancestor of `/tmp` on
    this dev machine."""
    base = Path.home() / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_rtb_provider_"))
    try:
        yield root
    finally:
        shutil.rmtree(root)


def test_reconstruct_temporal_backend_resets_provider_endpoints(
    isolated_tmp_root,
) -> None:
    repo_path = isolated_tmp_root / "activated-repos" / "user1" / "myrepo"
    config_dir = repo_path / ".code-indexer"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "codebase_dir": str(repo_path),
                "embedding_provider": "voyage-ai",
                "voyage_ai": {"api_endpoint": _REPO_CONFIGURED_VOYAGE_ENDPOINT},
                "cohere": {"api_endpoint": _REPO_CONFIGURED_COHERE_ENDPOINT},
            }
        )
    )

    with patch(
        "code_indexer.backends.backend_factory.BackendFactory.create",
        return_value=MagicMock(),
    ):
        config, _index_path, _vector_store = reconstruct_temporal_backend(
            repo_path=repo_path,
            repository_alias="myrepo",
        )

    assert config.voyage_ai.api_endpoint == VoyageAIConfig().api_endpoint, (
        "SECURITY: reconstruct_temporal_backend must reset "
        "voyage_ai.api_endpoint to the server-managed default before the "
        f"per-commit embedder is constructed. Got {config.voyage_ai.api_endpoint!r}"
    )
    assert config.cohere.api_endpoint == CohereConfig().api_endpoint, (
        "SECURITY: reconstruct_temporal_backend must reset "
        "cohere.api_endpoint to the server-managed default. Got "
        f"{config.cohere.api_endpoint!r}"
    )
