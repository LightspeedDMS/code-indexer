"""Bug #1979: shared "was every configured provider genuinely rebuilt this
run" check.

clear=true is documented as "index from scratch" -- every configured
embedding provider must come out of a successful clear=true run with a
fresh, populated collection. A provider can silently fail to be rebuilt
for many distinct reasons (no API key, a failed authenticating health
check, or any future reason) -- matching specific stdout wording for each
reason is fragile and was proven to miss real cases across two review
rounds (Bug #1979 turns 13 and 15). The single robust, reason-agnostic
signal is each provider's OWN completed progress metadata file
(`metadata-<provider>.json`, the same naming convention
`cli.py`'s `_get_provider_metadata_path` already uses): if it was not
marked completed and updated to a timestamp at or after the clear operation started, that
provider was not genuinely rebuilt this run -- regardless of whether its
collection happens to still hold old points from a prior run.

Both the CLI (`cli.py`'s `index` command, standalone `--clear`) and the
server (`ActivatedRepoIndexManager._execute_semantic_indexing`, activated
repo `clear=true`) call this SAME function so the two front doors enforce
one identical rule instead of two independently-maintained copies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from code_indexer.config import Config
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def find_providers_not_rebuilt_since(
    config_dir: Path,
    provider_names: List[str],
    since_timestamp: float,
    config: Optional[Config] = None,
    vector_store: Optional[FilesystemVectorStore] = None,
    daemon_mode: bool = False,
) -> List[str]:
    """Return the subset of `provider_names` NOT genuinely rebuilt since
    `since_timestamp`.

    A provider is considered "not rebuilt" when its metadata file is
    missing, unreadable, has a status other than `completed`, or carries a
    `last_index_timestamp` older than `since_timestamp`.
    When both optional arguments are supplied, its configured TEXT
    collection must also contain at least one committed row. Multimodal collections
    may be absent or empty after a clear.

    Args:
        config_dir: The repo's `.code-indexer` directory (where metadata
            files live).
        provider_names: The full list of configured providers (e.g. from
            `Config.get_embedding_providers()`).
        since_timestamp: A `time.time()` value captured before the clear
            operation started.
        config: Repository configuration used to resolve each provider's
            TEXT model, or None for the legacy timestamp-only check.
        vector_store: Store used to resolve the expected TEXT collection, or None
            for the legacy timestamp-only check.
        daemon_mode: When True, read the daemon's own bare
            `config_dir / "metadata.json"` instead of the per-provider
            `config_dir / f"metadata-{provider_name}.json"` file used by the
            foreground CLI (`_get_provider_metadata_path`) and server call
            sites. Bug #1979 (round 4): the daemon writes the bare legacy
            filename (reverted from a round-3 per-provider write -- see
            `daemon/service.py`), so the daemon-mode caller in `cli.py`
            (after `_index_via_daemon` returns) must check THAT file
            instead. Safe because a daemon-mode `--clear` with more than one
            configured provider is already rejected before delegation, so
            `provider_names` is always exactly one entry here -- no
            per-provider ambiguity to resolve against a single bare file.

    Returns:
        Provider names that were not genuinely rebuilt, in input order.
    """
    stale_or_missing: List[str] = []
    if (config is None) != (vector_store is None):
        raise ValueError("config and vector_store must be provided together")
    for provider_name in provider_names:
        metadata_path = (
            config_dir / "metadata.json"
            if daemon_mode
            else config_dir / f"metadata-{provider_name}.json"
        )
        if not metadata_path.exists():
            stale_or_missing.append(provider_name)
            continue
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            stale_or_missing.append(provider_name)
            continue
        if metadata.get("status") != "completed":
            stale_or_missing.append(provider_name)
            continue
        last_indexed = metadata.get("last_index_timestamp", 0.0)
        if not isinstance(last_indexed, (int, float)) or last_indexed < since_timestamp:
            stale_or_missing.append(provider_name)
            continue
        if config is not None and vector_store is not None:
            from code_indexer.services.embedding_factory import EmbeddingProviderFactory
            from code_indexer.services.temporal.temporal_row_existence import (
                temporal_shard_has_committed_rows,
            )

            provider = EmbeddingProviderFactory.create(
                config, provider_name=provider_name
            )
            text_collection = vector_store.resolve_collection_name(config, provider)
            collection_path = vector_store._get_collection_path(text_collection)
            # This existing layout-aware, read-only helper also works for
            # ordinary TEXT collections; cached HNSW counts can be stale.
            if not temporal_shard_has_committed_rows(
                collection_path, on_error="treat_absent"
            ):
                stale_or_missing.append(provider_name)
    return stale_or_missing
