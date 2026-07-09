"""Token preflight + request-seal utilities for per-commit temporal indexing.

Story #1290 AC23: after the aggregated per-commit document has been chunked
by CHARACTERS (fixed-size, 0% overlap for the contextual embedder), the
adapter preflights provider token/request limits BEFORE calling the
embedding API:

- A single chunk whose ESTIMATED tokens exceed the provider per-chunk cap is
  split DETERMINISTICALLY (never re-chunked by content/whitespace boundaries
  -- pure character-count arithmetic, matching FixedSizeChunker's philosophy),
  or fails loud with the commit hash + path when it genuinely cannot converge.
- The request-level seal (max documents / max total chunks / max total
  tokens per request) is enforced by grouping documents into sub-batches.

These are pure functions with no I/O so they are trivially unit-testable and
reusable by any TemporalEmbedder adapter.
"""

import math
from typing import Callable, List

# Provider request-level seal for the contextualized-embeddings endpoint --
# a fixed provider API contract (Story #1290 AC23), not a deployment-tunable
# knob, so these are module constants rather than config fields.
MAX_DOCUMENTS_PER_REQUEST = 1000
MAX_CHUNKS_PER_REQUEST = 16000
MAX_TOKENS_PER_REQUEST = 120_000

# Bounded recursion depth for preflight_split_chunk: each level halves the
# remaining piece count needed, so 20 levels covers an astronomically large
# ratio between max_tokens_per_chunk and the estimated token count. Anti-
# Unbounded-Loop (Messi #14): this bound is the proof of termination.
_MAX_SPLIT_DEPTH = 20


def preflight_split_chunk(
    text: str,
    count_tokens: Callable[[str], int],
    max_tokens_per_chunk: int,
    *,
    context_label: str = "",
    _depth: int = 0,
) -> List[str]:
    """Split `text` deterministically until each piece's estimated tokens fit.

    Splits evenly by CHARACTER COUNT into ceil(tokens/max_tokens_per_chunk)
    pieces (no whitespace/content-boundary search -- this must work even on a
    long no-whitespace diff). Recurses (bounded by _MAX_SPLIT_DEPTH) if a
    resulting piece is still over the cap after one pass.

    Args:
        text: Chunk text to preflight.
        count_tokens: Token-estimation callable (e.g. the provider's accurate
            tokenizer, or a cheap char/4 fallback).
        max_tokens_per_chunk: Provider per-chunk token cap.
        context_label: Human-readable context (commit hash + path) included
            in the fail-loud error if splitting cannot converge.
        _depth: Internal recursion guard -- callers should not set this.

    Returns:
        Ordered list of text pieces whose concatenation equals `text`, each
        estimated at or under max_tokens_per_chunk tokens.

    Raises:
        RuntimeError: If splitting cannot converge within _MAX_SPLIT_DEPTH
            levels (a single character exceeds the cap on its own -- an
            effectively impossible but defensively-guarded case). Includes
            `context_label` so the caller can identify the offending commit
            and path.
    """
    if not text:
        return []

    tokens = count_tokens(text)
    if tokens <= max_tokens_per_chunk:
        return [text]

    if _depth >= _MAX_SPLIT_DEPTH or len(text) <= 1:
        raise RuntimeError(
            f"Cannot split chunk under the {max_tokens_per_chunk}-token cap "
            f"(estimated {tokens} tokens, {len(text)} chars) for {context_label}"
        )

    num_pieces = max(2, math.ceil(tokens / max_tokens_per_chunk))
    piece_len = max(1, math.ceil(len(text) / num_pieces))

    raw_pieces = [text[i : i + piece_len] for i in range(0, len(text), piece_len)]

    result: List[str] = []
    for piece in raw_pieces:
        if count_tokens(piece) <= max_tokens_per_chunk:
            result.append(piece)
        else:
            result.extend(
                preflight_split_chunk(
                    piece,
                    count_tokens,
                    max_tokens_per_chunk,
                    context_label=context_label,
                    _depth=_depth + 1,
                )
            )
    return result


def pack_chunks_into_documents(
    chunks: List[str],
    count_tokens: Callable[[str], int],
    *,
    max_chunks_per_document: int,
    max_tokens_per_document: int,
) -> List[List[str]]:
    """Pack a FLAT ordered chunk list into minimal sequential document groups.

    Complements ``enforce_request_seal`` (which groups whole DOCUMENTS into
    request-level batches): this packs individual CHUNKS -- already produced
    by ``preflight_split_chunk`` for one commit's aggregated document -- into
    document-sized groups, sealing the current group BEFORE the next chunk
    would push it over either the per-document chunk-count or token-sum cap.
    This is the same dual-constraint greedy-sealing shape used by the
    embedding request coalescer (Story #1079), applied at chunk granularity
    so a single oversized commit can be split into multiple contextualized-
    embeddings documents/requests while a normal commit still yields exactly
    ONE document (preserving full intra-document context in the common case).

    Args:
        chunks: Ordered chunk texts for one commit (already preflight-split
            per individual chunk).
        count_tokens: Token-estimation callable.
        max_chunks_per_document: Max chunk count per packed document.
        max_tokens_per_document: Max total estimated tokens per packed
            document.

    Returns:
        Ordered list of documents (each a list of chunk texts); concatenating
        all documents in order reproduces `chunks` exactly. A single chunk
        whose own token count already exceeds the cap is still emitted alone
        in its own document (best effort, mirrors enforce_request_seal).
    """
    if not chunks:
        return []

    documents: List[List[str]] = []
    current_doc: List[str] = []
    current_tokens = 0

    for chunk in chunks:
        chunk_tokens = count_tokens(chunk)
        would_exceed = (
            len(current_doc) + 1 > max_chunks_per_document
            or current_tokens + chunk_tokens > max_tokens_per_document
        )
        if would_exceed and current_doc:
            documents.append(current_doc)
            current_doc = []
            current_tokens = 0

        current_doc.append(chunk)
        current_tokens += chunk_tokens

    if current_doc:
        documents.append(current_doc)

    return documents


def enforce_request_seal(
    documents: List[List[str]],
    count_tokens: Callable[[str], int],
    *,
    max_documents: int = MAX_DOCUMENTS_PER_REQUEST,
    max_chunks: int = MAX_CHUNKS_PER_REQUEST,
    max_tokens: int = MAX_TOKENS_PER_REQUEST,
) -> List[List[List[str]]]:
    """Group per-commit documents into request-seal-respecting sub-batches.

    Preserves the original document order across the flattened output. A
    single document whose own chunk count or token total already exceeds a
    cap is still emitted alone in its own sub-batch (best effort -- callers
    that need per-chunk splitting should run preflight_split_chunk on
    individual chunks before calling this).

    Args:
        documents: Ordered list of documents; each document is an ordered
            list of already-chunked text.
        count_tokens: Token-estimation callable.
        max_documents: Max documents per request-level sub-batch.
        max_chunks: Max total chunks (summed across documents) per sub-batch.
        max_tokens: Max total estimated tokens (summed across all chunks in
            all documents) per sub-batch.

    Returns:
        Ordered list of sub-batches; each sub-batch is a list of documents
        (list of chunk-lists) satisfying all three seal limits, except a
        single lone oversized document which is emitted alone.
    """
    if not documents:
        return []

    groups: List[List[List[str]]] = []
    current_group: List[List[str]] = []
    current_chunks = 0
    current_tokens = 0

    for doc in documents:
        doc_chunk_count = len(doc)
        doc_tokens = sum(count_tokens(chunk) for chunk in doc)

        would_exceed = (
            len(current_group) + 1 > max_documents
            or current_chunks + doc_chunk_count > max_chunks
            or current_tokens + doc_tokens > max_tokens
        )
        if would_exceed and current_group:
            groups.append(current_group)
            current_group = []
            current_chunks = 0
            current_tokens = 0

        current_group.append(doc)
        current_chunks += doc_chunk_count
        current_tokens += doc_tokens

    if current_group:
        groups.append(current_group)

    return groups
