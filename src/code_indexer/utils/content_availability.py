"""The "chunk content could not be read" signal (Bug #1991).

FilesystemVectorStore sets ``staleness[CONTENT_UNAVAILABLE_KEY] = True`` (with
empty content) when neither the working file nor the git blob can be read.
Every consumer -- server result conversion, CLI/daemon staleness annotation,
CLI display -- reads the signal through :func:`is_content_unavailable`, so a
failed read is never shown as if it were (empty) code.
"""

from typing import Any, Dict, Mapping

CONTENT_UNAVAILABLE_KEY = "content_unavailable"

# Shown by the CLI in place of the snippet of an unreadable chunk.
CONTENT_UNAVAILABLE_MARKER = "[content unavailable: file could not be read]"


def is_content_unavailable(result: Mapping[str, Any]) -> bool:
    """True when a raw vector-store search result carries the signal."""
    staleness = result.get("staleness")
    return isinstance(staleness, Mapping) and (
        staleness.get(CONTENT_UNAVAILABLE_KEY) is True
    )


def staleness_after_local_check(
    result: Mapping[str, Any], local_staleness: Dict[str, Any]
) -> Dict[str, Any]:
    """Staleness to attach after the CLI/daemon local (mtime) check.

    The local check only compares timestamps; it cannot know the content
    was unreadable, so the store's unavailable state takes precedence.
    """
    if is_content_unavailable(result):
        return dict(result["staleness"])
    return local_staleness
