"""
Bug: `_on_config_change` (lifespan.py, Bug #586 block) never syncs a Cohere
API key change to `os.environ["CO_API_KEY"]` without a full server restart.

Two independent, compounding defects in the SAME block:

1. **Missing call** (the reported bug): `sync_svc.sync_cohere_key(...)` is
   never called at all -- only anthropic and voyage are synced. A Cohere key
   set/rotated through any path that reaches this config-change callback
   (as opposed to the direct `POST /api/api-keys/cohere` REST route, which
   DOES call `sync_cohere_key` correctly) never reaches `os.environ` until
   the next full server restart.

2. **Pre-existing typo bug in the voyage line, discovered while fixing #1**:
   the block reads `ci.voyage_api_key`, but `ClaudeIntegrationConfig` (see
   `server/utils/config_manager.py`) has no such field -- only
   `voyageai_api_key`. Evaluating `ci.voyage_api_key` raises `AttributeError`,
   which the enclosing `except Exception: logger.debug(...)` swallows
   silently. This means voyage-key sync-on-config-change has ALSO never
   worked, and naively appending a cohere call AFTER this line (mirroring it
   verbatim, typo included) would be unreachable dead code -- the crash on
   the voyage line happens first, every time.

Test suite (mirrors the established pattern in
test_lifespan_cache_hot_reload_cluster_gap_1399.py):
1. Source-text guards: `_on_config_change` must call `sync_cohere_key` and
   must use the CORRECT `voyageai_api_key` field name (never the broken
   `voyage_api_key`).
2. Runtime guard: replicates the exact (fixed) API-key-sync block with a
   real `ApiKeySyncService` and a real `ClaudeIntegrationConfig`, proving
   both keys land in `os.environ`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


def _extract_on_config_change_block(source: str) -> str:
    """Return the _on_config_change function body slice for source-guard checks."""
    start = source.find("def _on_config_change(new_config: Any) -> None:")
    assert start != -1, "_on_config_change callback not found in lifespan.py"
    end = source.find("_get_cs().register_on_change_callback(_on_config_change)")
    assert end != -1, (
        "_get_cs().register_on_change_callback(_on_config_change) registration "
        "not found in lifespan.py"
    )
    return source[start:end]


class TestSourceGuards:
    def test_on_config_change_syncs_cohere_key(self):
        """_on_config_change must call sync_svc.sync_cohere_key(ci.cohere_api_key)
        guarded by `if ci and ci.cohere_api_key:`, mirroring the existing
        anthropic/voyage sync calls in the same block -- otherwise a Cohere
        key change via this callback path never reaches os.environ without a
        full server restart."""
        source = _LIFESPAN_PATH.read_text()
        block = _extract_on_config_change_block(source)

        assert "ci.cohere_api_key" in block and "sync_cohere_key" in block, (
            "_on_config_change must call "
            "sync_svc.sync_cohere_key(ci.cohere_api_key) so a Cohere API key "
            "configured/rotated through this callback path is synced to "
            "os.environ without requiring a full server restart."
        )

    def test_on_config_change_uses_correct_voyageai_field_name(self):
        """The voyage sync line must reference the REAL ClaudeIntegrationConfig
        field `voyageai_api_key`, never the nonexistent `voyage_api_key` --
        the latter raises AttributeError, silently swallowed by the
        enclosing except-Exception, which kills voyage sync-on-config-change
        (and would kill any code appended after it, e.g. a cohere sync call)."""
        source = _LIFESPAN_PATH.read_text()
        block = _extract_on_config_change_block(source)

        assert "ci.voyage_api_key" not in block, (
            "_on_config_change references the nonexistent "
            "ClaudeIntegrationConfig.voyage_api_key attribute -- this raises "
            "AttributeError (silently swallowed by the enclosing "
            "except-Exception), which means voyage sync-on-config-change has "
            "never actually run, and any code appended after this line is "
            "unreachable dead code."
        )
        assert "ci.voyageai_api_key" in block, (
            "_on_config_change must reference the real "
            "ClaudeIntegrationConfig.voyageai_api_key field."
        )


# ---------------------------------------------------------------------------
# Runtime guard
# ---------------------------------------------------------------------------


@pytest.fixture
def _restore_provider_env_vars() -> Iterator[None]:
    """Snapshot and restore VOYAGE_API_KEY/CO_API_KEY around the test so this
    test never leaks a fake key into the rest of the suite."""
    saved = {k: os.environ.get(k) for k in ("VOYAGE_API_KEY", "CO_API_KEY")}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class TestOnConfigChangeApiKeySyncRuntime:
    """Runtime guard: replicates the exact (fixed) _on_config_change
    API-key-sync block against a REAL ApiKeySyncService and a REAL
    ClaudeIntegrationConfig object -- only the ~/.bashrc filesystem side
    effect is mocked (a legitimate isolation boundary: this must never
    write to the real developer's home directory in a unit test)."""

    def test_cohere_and_voyage_keys_synced_to_environ(
        self, _restore_provider_env_vars: None
    ) -> None:
        from code_indexer.server.services.api_key_management import (
            ApiKeySyncService,
        )
        from code_indexer.server.utils.config_manager import (
            ClaudeIntegrationConfig,
        )

        os.environ.pop("VOYAGE_API_KEY", None)
        os.environ.pop("CO_API_KEY", None)

        sync_svc = ApiKeySyncService()
        ci = ClaudeIntegrationConfig(
            voyageai_api_key="fresh-voyage-key",
            cohere_api_key="fresh-cohere-key",
        )

        with patch.object(ApiKeySyncService, "_update_bashrc"):
            # --- Replicate the fixed _on_config_change API-key-sync block ---
            if ci and ci.voyageai_api_key:
                sync_svc.sync_voyageai_key(ci.voyageai_api_key)
            if ci and ci.cohere_api_key:
                sync_svc.sync_cohere_key(ci.cohere_api_key)

        assert os.environ.get("VOYAGE_API_KEY") == "fresh-voyage-key", (
            "sync_voyageai_key must update os.environ['VOYAGE_API_KEY']."
        )
        assert os.environ.get("CO_API_KEY") == "fresh-cohere-key", (
            "sync_cohere_key must update os.environ['CO_API_KEY'] -- this is "
            "the exact production path a Cohere key change via "
            "_on_config_change must exercise."
        )
