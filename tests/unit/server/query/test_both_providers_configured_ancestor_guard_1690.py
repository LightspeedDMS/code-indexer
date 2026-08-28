"""Bug #1690: SemanticQueryManager._both_providers_configured blind-trusted
ConfigManager.create_with_backtrack(repo_path).get_config() without
verifying the resolved config genuinely describes repo_path itself --
mirroring the exact root-cause pattern Bug #1683 round 4 fixed for
AutoWatchManager.start_watch.

EmbeddingProviderFactory.get_configured_providers() reads
config.cohere.api_key DIRECTLY off the returned Config object (not just
env vars) -- so an ancestor's config.json with a real Cohere API key
configured for an UNRELATED repo would leak into this repo's dual-provider
strategy decision, silently routing a query to the wrong (dual-provider
RRF) strategy instead of the correct single-provider one.

The pre-existing except-Exception fallback (empty config on ANY load
failure, tolerating "incomplete local configs" by design -- see the
function's own docstring/comments) means routing through
ConfigManager.load_verified_config (Bug #1690) is a safe, behavior
-preserving fix: it raises ConfigVerificationError (a ValueError subclass)
on an ancestor-only or defaulted config, which the SAME existing
except-Exception branch degrades to the honest env-var-only fallback
instead of leaking the ancestor's real (misleading) config in.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from code_indexer.config import ConfigManager
from code_indexer.server.query.semantic_query_manager import SemanticQueryManager


@pytest.fixture
def isolated_tmp_root():
    """Rooted under `~/.tmp` (never bare `/tmp` -- project convention),
    immune to any real `.code-indexer/config.json` ancestor of `/tmp` on
    this dev machine."""
    base = Path.home() / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_1690_bpc_"))
    try:
        yield root
    finally:
        shutil.rmtree(root)


def _make_manager() -> SemanticQueryManager:
    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    import logging

    manager.logger = logging.getLogger("test")
    return manager


class TestBothProvidersConfiguredRefusesAncestorOnlyConfig:
    def test_does_not_leak_ancestors_cohere_key(
        self, isolated_tmp_root, monkeypatch
    ) -> None:
        # Env-var-based VoyageAI configuration (avoids the config.voyage_ai
        # attribute-access path entirely; unrelated to this bug).
        monkeypatch.setenv("VOYAGE_API_KEY", "env-voyage-key")
        monkeypatch.delenv("CO_API_KEY", raising=False)

        ancestor_dir = isolated_tmp_root / "server-data"
        ancestor_dir.mkdir()
        ancestor_config_manager = ConfigManager(
            ancestor_dir / ".code-indexer" / "config.json"
        )
        ancestor_config = ancestor_config_manager.create_default_config(
            codebase_dir=ancestor_dir
        )
        ancestor_config.cohere.api_key = "ancestor-real-cohere-key"
        ancestor_config_manager.save(ancestor_config)

        # Dangling repo with no config of its own, nested under the
        # ancestor's real config.
        repo_path = ancestor_dir / "activated-repos" / "user1" / "myrepo"
        repo_path.mkdir(parents=True)

        manager = _make_manager()
        result = manager._both_providers_configured(str(repo_path))

        assert result is False, (
            "Must NOT report both providers configured based on an "
            "unrelated ANCESTOR repo's real Cohere API key -- this repo "
            "has no config of its own and only VoyageAI is configured "
            "(via env var)."
        )
