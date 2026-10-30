"""THE per-provider indexing metadata file of a repository: used by
`cidx index` (every provider) and the server's watch (#2093)."""

import shutil
from pathlib import Path


def provider_metadata_path(config_dir: Path, provider_name: str) -> Path:
    """Return per-provider metadata path with backward-compat migration.

    Returns config_dir / f"metadata-{provider_name}.json".

    Migration for voyage-ai: if metadata-voyage-ai.json does not exist but
    metadata.json does, the legacy file is copied (byte-for-byte) to the
    provider path so existing incremental state is preserved. New
    providers always receive a fresh metadata file, which forces a full
    index on first use.

    The copy carries the legacy status unchanged (Bug #1624 reverted,
    Story #2121): a stored in_progress/failed status routes the next run to
    a reconcile of the store against disk, so normalizing it would skip
    that recovery.
    """
    provider_metadata = config_dir / f"metadata-{provider_name}.json"
    if provider_metadata.exists():
        return provider_metadata

    legacy_metadata = config_dir / "metadata.json"
    if provider_name == "voyage-ai" and legacy_metadata.exists():
        shutil.copy2(str(legacy_metadata), str(provider_metadata))
        return provider_metadata

    return provider_metadata
