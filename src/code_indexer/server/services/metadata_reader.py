"""Provider-aware metadata reader for dep-map services (Bug #890)."""

import json
import logging
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Union

logger = logging.getLogger(__name__)


class IndexState(NamedTuple):
    """One metadata file's view of the last indexing run."""

    source: str  # metadata file name, e.g. "metadata-cohere.json"
    status: Optional[str]
    current_commit: Optional[str]


def read_index_states(clone_path: Union[str, Path]) -> List[IndexState]:
    """Return the indexing state recorded by EVERY provider of a repository.

    `cidx index` writes one `.code-indexer/metadata-{provider}.json` per
    configured provider (removing a provider deletes its file), so each
    provider file present is one state, in file-name order. The legacy bare
    `metadata.json` is read only when no provider file exists. Unreadable
    fields are None (see _read_key_from_file); never raises.
    """
    return [
        IndexState(
            source=path.name,
            status=_read_key_from_file(path, "status"),
            current_commit=_read_key_from_file(path, "current_commit"),
        )
        for path in _metadata_paths(clone_path)
    ]


def snapshot_index_metadata(clone_path: Union[str, Path]) -> Dict[str, bytes]:
    """Raw bytes of every metadata file read_index_states() reads, by name
    (taken before an indexing run, for index_unchanged_since())."""
    return {path.name: path.read_bytes() for path in _metadata_paths(clone_path)}


def index_unchanged_since(
    clone_path: Union[str, Path], before: Dict[str, bytes]
) -> bool:
    """True when no indexing run since ``before`` changed the index.

    Every finished `cidx index` run rewrites its provider's metadata file
    with a new ``run_sequence`` and ``last_run_changed_index``, so a file
    byte-identical to ``before`` had no finished run (e.g. a provider that
    was skipped). A rewritten file counts as unchanged only when it records
    ``last_run_changed_index`` exactly False; a file that cannot be read or
    parsed, or lacks the flag, counts as changed (the caller publishes).
    """
    for path in _metadata_paths(clone_path):
        try:
            current = path.read_bytes()
        except OSError as exc:
            logger.warning("metadata_reader: cannot read %s: %s", path, exc)
            return False
        if before.get(path.name) == current:
            continue
        try:
            data = json.loads(current)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("metadata_reader: unreadable %s: %s", path, exc)
            return False
        if (
            not isinstance(data, dict)
            or data.get("last_run_changed_index") is not False
        ):
            return False
    return True


def _metadata_paths(clone_path: Union[str, Path]) -> List[Path]:
    """Provider metadata files in name order; the legacy bare metadata.json
    only when no provider file exists."""
    if not isinstance(clone_path, (str, Path)):
        raise TypeError(
            f"clone_path must be str or Path, got {type(clone_path).__name__}"
        )
    code_indexer_dir = Path(clone_path) / ".code-indexer"
    paths = sorted(code_indexer_dir.glob("metadata-*.json"))
    if paths:
        return paths
    legacy_path = code_indexer_dir / "metadata.json"
    return [legacy_path] if legacy_path.exists() else []


def _read_key_from_file(metadata_path: Path, key: str) -> Optional[str]:
    """Read a single string field from a metadata file.

    Bug #1623-B: shared implementation for read_index_states() and
    read_current_commit(), collapsing the former per-key copies
    (_read_status_from_file / _read_commit_from_file) that differed only in
    which dict key was read (Messi Rule #4, anti-duplication).

    Returns the value for `key` if present, non-empty, and a str.
    Returns None on read error, parse error, wrong JSON type, missing key,
    empty value, or non-string value — never raises.
    """
    try:
        data = json.loads(metadata_path.read_text())
    except OSError as exc:
        logger.warning("metadata_reader: cannot read %s: %s", metadata_path, exc)
        return None
    except json.JSONDecodeError as exc:
        logger.warning("metadata_reader: malformed JSON in %s: %s", metadata_path, exc)
        return None
    except UnicodeDecodeError as exc:
        logger.warning("metadata_reader: cannot decode %s: %s", metadata_path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning(
            "metadata_reader: expected JSON object in %s, got %s",
            metadata_path,
            type(data).__name__,
        )
        return None
    value = data.get(key)
    if not isinstance(value, str) or not value:
        return None
    return value


def read_current_commit(clone_path: Union[str, Path]) -> Optional[str]:
    """Return current_commit SHA from provider-suffixed metadata, legacy fallback.

    Bug #890: Prefers `.code-indexer/metadata-voyage-ai.json` (written by
    cidx index since the provider-aware migration). Falls back to legacy
    `.code-indexer/metadata.json` only if the voyage file is entirely absent.

    If the voyage file exists but is malformed, missing the key, or has an
    empty/non-string value, returns None immediately without consulting the
    legacy file.

    Args:
        clone_path: Path (str or Path) to the repository's base clone directory.
            Must not be None.

    Returns:
        The current_commit SHA string, or None when no valid metadata is found.

    Raises:
        TypeError: if clone_path is None or not a str/Path.
    """
    if clone_path is None:
        raise TypeError("clone_path must be str or Path, got None")
    if not isinstance(clone_path, (str, Path)):
        raise TypeError(
            f"clone_path must be str or Path, got {type(clone_path).__name__}"
        )

    code_indexer_dir = Path(clone_path) / ".code-indexer"

    voyage_path = code_indexer_dir / "metadata-voyage-ai.json"
    if voyage_path.exists():
        return _read_key_from_file(voyage_path, "current_commit")

    legacy_path = code_indexer_dir / "metadata.json"
    if legacy_path.exists():
        return _read_key_from_file(legacy_path, "current_commit")

    return None
