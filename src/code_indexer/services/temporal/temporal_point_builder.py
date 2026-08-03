"""Unified point_id + payload builder for per-commit temporal chunks.

Story #1290 AC3, AC5, AC12: a single point_id scheme replaces the legacy
":diff:" and standalone commit-message ids, and the payload carries the
canonical `type == "commit_chunk"` + `is_head` fields with `commit_message`
populated ONLY on the head chunk (chunk_index 0).
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from .contextual_chunker import AggregatedChunk
from .models import CommitInfo

DEFAULT_COMMIT_MESSAGE_CAP = 200


def build_point_id(project_id: str, commit_hash: str, chunk_index: int) -> str:
    """Return the unified point_id: "{project_id}:commit:{hash}:{j}"."""
    return f"{project_id}:commit:{commit_hash}:{chunk_index}"


def parse_temporal_point_id(point_id: str) -> Optional[Tuple[str, str, int]]:
    """Parse the unified point_id scheme back into its components.

    Story #1493 AC2 (report Finding C2): this is a ZERO-I/O, zero-decode
    projection -- the point_id string ALREADY carries the chunk_index that
    build_point_id() encoded, so recovering it needs no chunk-store read at
    all. Mirrors the same "split(':') / len==4 / parts[1]=='commit'" shape
    already used inline at the three existing point_id parse call sites
    (temporal_reconciliation.py, temporal_consolidated_build.py) -- this is
    the canonical, reusable counterpart to build_point_id().

    Returns:
        (project_id, commit_hash, chunk_index) tuple, or None when point_id
        does not match the expected "{project_id}:commit:{hash}:{j}" shape
        (e.g. a non-temporal collection's point_id, or a corrupt/malformed
        value). Callers MUST treat None as "cannot determine" and fall back
        to a full decode -- NEVER as a negative match (project anti-silent-
        failure rule: an unparseable id must not silently masquerade as a
        confirmed non-match).
    """
    parts = point_id.split(":")
    if len(parts) != 4 or parts[1] != "commit":
        return None
    try:
        chunk_index = int(parts[3])
    except ValueError:
        return None
    return parts[0], parts[2], chunk_index


def is_head_chunk_id(point_id: str) -> Optional[bool]:
    """Return whether `point_id` is the head (chunk_index == 0) chunk of its
    commit -- derived PURELY from the point_id string.

    By construction (contextual_chunker.py's `is_head = (idx == 0)`, and
    build_point_id()'s `chunk_index` parameter being that SAME `idx`), this
    is byte-identical to the stored payload's `is_head` field -- not an
    approximation. Story #1493 AC2 uses this to let FilesystemVectorStore's
    hydration loop skip a full zstd+json decode for any HNSW candidate a
    chunk_type=="commit_message"/"commit_diff" post-filter would discard
    anyway, without touching the chunk store at all.

    Returns:
        True/False when point_id parses as the unified temporal scheme.
        None when it does not -- callers MUST fall back to a full decode
        rather than treat None as a non-match (see parse_temporal_point_id).
    """
    parsed = parse_temporal_point_id(point_id)
    if parsed is None:
        return None
    _project_id, _commit_hash, chunk_index = parsed
    return chunk_index == 0


def short_cap_commit_message(
    message: Optional[str], cap: int = DEFAULT_COMMIT_MESSAGE_CAP
) -> str:
    """Return `message` truncated to `cap` characters (empty string for falsy input)."""
    if not message:
        return ""
    return message[:cap]


def build_chunk_payload(
    commit: CommitInfo, chunk: AggregatedChunk, project_id: str
) -> Dict[str, Any]:
    """Build the payload dict for one aggregated-document chunk.

    Args:
        commit: The commit this chunk belongs to.
        chunk: The chunk (from contextual_chunker.chunk_aggregated_document()).
        project_id: Project identifier (see FileIdentifier.get_project_id()).

    Returns:
        Payload dict with canonical `type`/`is_head` fields, provenance
        `paths`/`primary_path`, and `commit_message` populated ONLY when
        `chunk.chunk_index == 0` (the head chunk).
    """
    commit_date = datetime.fromtimestamp(commit.timestamp, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )
    return {
        "type": "commit_chunk",
        "is_head": chunk.is_head,
        "commit_hash": commit.hash,
        "commit_timestamp": commit.timestamp,
        "commit_date": commit_date,
        "author_name": commit.author_name,
        "author_email": commit.author_email,
        "paths": chunk.paths,
        "primary_path": chunk.primary_path,
        "chunk_index": chunk.chunk_index,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "project_id": project_id,
        "commit_message": (
            short_cap_commit_message(commit.message) if chunk.chunk_index == 0 else ""
        ),
    }
