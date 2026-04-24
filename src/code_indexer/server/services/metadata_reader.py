"""Provider-aware metadata reader for dep-map services (Bug #890)."""

import json
import logging
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)


def _read_commit_from_file(metadata_path: Path) -> Optional[str]:
    """Read current_commit from a single metadata file.

    Returns the commit string if present, non-empty, and a str.
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
    commit = data.get("current_commit")
    if not isinstance(commit, str) or not commit:
        return None
    return commit


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
        return _read_commit_from_file(voyage_path)

    legacy_path = code_indexer_dir / "metadata.json"
    if legacy_path.exists():
        return _read_commit_from_file(legacy_path)

    return None
