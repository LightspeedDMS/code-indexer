"""Zero-overlap contextual chunker for per-commit aggregated documents.

Story #1290 (AC1, AC2, AC6, AC26): chunks an AggregatedCommitDocument by
CHARACTERS with 0% overlap (per-adapter -- the contextual embedder uses 0%;
a standard/Cohere adapter would use a different overlap on the SAME
aggregated document, which is why chunking lives here rather than in
commit_aggregator.py). Zero overlap means `next_start = previous_end` -- the
exact vector count is `ceil(len(text) / chunk_chars)`, matching AC1/AC2's
deterministic formula.

Each chunk also carries `paths[]`/`primary_path` (derived from the
aggregator's section-range provenance map) and `is_head` (chunk_index == 0 --
the message always leads the aggregated document, so the first chunk is
always the "head" chunk).
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .commit_aggregator import AggregatedCommitDocument


@dataclass(frozen=True)
class AggregatedChunk:
    """One fixed-size, zero-overlap chunk of an aggregated commit document."""

    text: str
    chunk_index: int
    char_start: int
    char_end: int
    is_head: bool
    paths: List[str]
    primary_path: Optional[str]


def _overlap_len(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    """Length of the overlap between [a_start, a_end) and [b_start, b_end)."""
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def _paths_and_primary(
    doc: AggregatedCommitDocument, start: int, end: int
) -> Tuple[List[str], Optional[str]]:
    """Return (paths, primary_path) for the sections overlapping [start, end).

    `paths` preserves provenance order (== the order files were aggregated).
    `primary_path` is the path with the greatest overlap length; ties are
    broken by first-encountered order (deterministic).
    """
    paths: List[str] = []
    best_path: Optional[str] = None
    best_overlap = -1
    for section in doc.provenance:
        if section.path is None:
            continue
        overlap = _overlap_len(start, end, section.start, section.end)
        if overlap <= 0:
            continue
        paths.append(section.path)
        if overlap > best_overlap:
            best_overlap = overlap
            best_path = section.path
    return paths, best_path


def chunk_aggregated_document(
    doc: AggregatedCommitDocument, chunk_chars: int
) -> List[AggregatedChunk]:
    """Chunk `doc.text` into fixed-size, 0%-overlap pieces.

    Args:
        doc: Aggregated per-commit document with its provenance map.
        chunk_chars: Chunk size in characters (TemporalConfig.aggregation_chunk_chars).

    Returns:
        Ordered list of AggregatedChunk covering doc.text with zero overlap
        and zero gaps; empty list for an empty document.
    """
    text = doc.text
    if not text:
        return []

    chunks: List[AggregatedChunk] = []
    pos = 0
    idx = 0
    n = len(text)
    while pos < n:
        end = min(pos + chunk_chars, n)
        paths, primary_path = _paths_and_primary(doc, pos, end)
        chunks.append(
            AggregatedChunk(
                text=text[pos:end],
                chunk_index=idx,
                char_start=pos,
                char_end=end,
                is_head=(idx == 0),
                paths=paths,
                primary_path=primary_path,
            )
        )
        pos = end  # 0% overlap
        idx += 1

    return chunks
