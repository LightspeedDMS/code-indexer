"""Unified point_id + payload builder for per-commit temporal chunks.

Story #1290 AC3, AC5, AC12: a single point_id scheme replaces the legacy
":diff:" and standalone commit-message ids, and the payload carries the
canonical `type == "commit_chunk"` + `is_head` fields with `commit_message`
populated ONLY on the head chunk (chunk_index 0).
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .contextual_chunker import AggregatedChunk
from .models import CommitInfo

DEFAULT_COMMIT_MESSAGE_CAP = 200


def build_point_id(project_id: str, commit_hash: str, chunk_index: int) -> str:
    """Return the unified point_id: "{project_id}:commit:{hash}:{j}"."""
    return f"{project_id}:commit:{commit_hash}:{chunk_index}"


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
