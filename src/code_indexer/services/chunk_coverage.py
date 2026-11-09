"""Chunk coverage of the text index (issue-64616).

Until issue-64616 a chunk that referenced an image went to the multimodal path
only, so its text never reached the text collection: with multimodal indexing
off (issue-65484) such a chunk was embedded nowhere, and with it on, a chunk
whose images could not be resolved or embedded was dropped. The text
collection now holds every chunk of every indexed file
(``FileChunkingManager``), so a file whose stored chunk indexes do not cover
its ``total_chunks`` was indexed under the old routing.

Reconcile compares whole files, so it never revisited such a file. Each index
records ``CHUNK_COVERAGE_KEY``: below ``CHUNK_COVERAGE_VERSION`` the next run
reads the stored payloads once (no vectors, no re-chunking), re-processes only
the incomplete files -- their stored chunks are reused by content hash, so
only the missing chunks are embedded -- and records the current version. A
complete index records it without processing anything. Bump the version only
for a change that leaves already-indexed files incomplete again.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Set, Tuple

#: Version of the chunk routing an index's stored files were checked against.
CHUNK_COVERAGE_VERSION = 1
#: Index metadata key holding the version the store was last checked against.
CHUNK_COVERAGE_KEY = "chunk_coverage_version"


def recorded_coverage_version(metadata: Mapping[str, Any]) -> int:
    """The chunk coverage version an index metadata records; 0 when absent or
    malformed (an index older than the check)."""
    value = metadata.get(CHUNK_COVERAGE_KEY)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def coverage_is_current(metadata: Mapping[str, Any]) -> bool:
    """Whether the index was checked against the current chunk routing."""
    return recorded_coverage_version(metadata) >= CHUNK_COVERAGE_VERSION


def incomplete_chunk_paths(
    points: Iterable[Mapping[str, Any]], current_branch: Optional[str]
) -> Set[str]:
    """Stored paths whose visible version lacks some of its chunks.

    Points are grouped by ``(path, file_hash)``: one version of a file. A
    point hidden on ``current_branch`` belongs to another branch's version and
    is ignored, so re-processing the file as it is now always clears it. A
    version is incomplete when its stored ``chunk_index`` values do not cover
    ``range(total_chunks)``. Points without an integer ``chunk_index`` and
    ``total_chunks`` are not judged."""
    seen: Dict[Tuple[str, Any], Set[int]] = {}
    totals: Dict[Tuple[str, Any], int] = {}
    for point in points:
        payload = point.get("payload") or {}
        path = payload.get("path")
        index = _count(payload.get("chunk_index"))
        total = _count(payload.get("total_chunks"))
        if not isinstance(path, str) or index is None or total is None:
            continue
        if current_branch and current_branch in (payload.get("hidden_branches") or []):
            continue
        key = (path, payload.get("file_hash"))
        seen.setdefault(key, set()).add(index)
        totals[key] = max(totals.get(key, 0), total)
    return {
        path
        for (path, file_hash), total in totals.items()
        if not set(range(total)) <= seen[(path, file_hash)]
    }


def _count(value: Any) -> Optional[int]:
    """``value`` when it is a non-negative int (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
