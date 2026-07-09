#!/usr/bin/env python3
"""Git-history-only temporal vector projection tool (Story #1292 / Epic #1289).

Computes, FROM GIT HISTORY ALONE -- no old-index read, no A/B (old-style)
index build -- the per-embedder projected vector count, file count, and
token/$ cost for the per-commit dual-embedder temporal layout (Story #1290 /
#1291), plus the legacy per-file-diff formula's `old_vectors` count for the
deleted OLD layout (computed analytically, NOT built by indexing the old
way). Writes a report to reports/perf/.

Reuses PRODUCTION code wherever possible so the projection is provably
identical to what real indexing produces:
  - commit_aggregator.get_file_changes / build_aggregated_document (the
    exact per-commit aggregation logic used by indexing).
  - contextual_chunker.chunk_aggregated_document (the exact per-adapter
    chunk-count logic; NEW-layout vector count IS the production chunk
    count -- not a reimplementation).
  - indexing.fixed_size_chunker.FixedSizeChunker.estimate_chunks (the exact
    legacy per-file chunk-count formula used by the deleted OLD per-file-diff
    temporal path).
  - The embedded, offline Voyage/Cohere tokenizers (no network, no API key
    required) for token estimates.

Usage:
    python3 scripts/analysis/temporal_vector_projection.py <repo> \\
        [--max-commits N] [--since-date YYYY-MM-DD] [--output-dir DIR] \\
        [--repo-label LABEL]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from code_indexer.config import IndexingConfig  # noqa: E402
from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker  # noqa: E402
from code_indexer.services.temporal.commit_aggregator import (  # noqa: E402
    AggregatedCommitDocument,
    FileChange,
    build_aggregated_document,
    get_file_changes,
)
from code_indexer.services.temporal.contextual_chunker import (  # noqa: E402
    chunk_aggregated_document,
)
from code_indexer.services.temporal.embedders.contextual import (  # noqa: E402
    ContextualTemporalEmbedder,
)
from code_indexer.services.temporal.embedders.standard import (  # noqa: E402
    StandardTemporalEmbedder,
)
from code_indexer.services.temporal.models import CommitInfo  # noqa: E402

# Legacy per-file-diff chunk size (Story #1292 AC2 technical detail): the
# deleted OLD temporal path chunked each file's diff with the SAME
# model-aware chunk size the regular indexer uses for voyage-code-3 /
# embed-v4.0 (FixedSizeChunker.MODEL_CHUNK_SIZES == 4096), at the standard
# 15% overlap (FixedSizeChunker.OVERLAP_PERCENTAGE). This matches
# temporal.aggregation_chunk_chars' own default (4096) for an apples-to-
# apples old-vs-new comparison.
LEGACY_CHUNK_CHARS = 4096
LEGACY_OVERLAP_PERCENTAGE = 0.15

# The two first-class temporal embedder adapters (Story #1290 / #1291).
# Referencing the CLASS attributes directly (never instantiating) avoids
# requiring any provider API key just to read name/model_slug/overlap -- this
# projection performs NO network calls (AC1: no old-index read, no A/B run).
_EMBEDDER_CLASSES = (ContextualTemporalEmbedder, StandardTemporalEmbedder)


# ---------------------------------------------------------------------------
# AC1: pricing read from a config/pricing source (YAML), not a bare literal.
# ---------------------------------------------------------------------------


def get_pricing_usd_per_million(embedder_name: str) -> float:
    """Return USD-per-million-tokens pricing for a temporal embedder.

    Reads from the same model-spec YAML the production embedding clients
    load (voyage_models.yaml / cohere_models.yaml) via their existing
    `_get_voyage_model_specs()` / `_get_cohere_model_specs()` loaders -- this
    is the "config/pricing source" required by AC1, not a CI-fragile literal
    baked directly into this script. A documented fallback constant (Voyage
    default $0.12/1M, Cohere's documented $0.12/1M text rate) is used ONLY if
    the YAML entry exists but omits the pricing field.

    Raises:
        KeyError: If `embedder_name` is not one of the two registered
            temporal embedder adapters (fail loud -- no silent default guess
            for an unrecognized name).
    """
    _FALLBACK = {"voyage-context-4": 0.12, "embed-v4.0": 0.12}
    if embedder_name not in _FALLBACK:
        raise KeyError(
            f"Unknown temporal embedder {embedder_name!r} -- no documented "
            f"pricing source. Known embedders: {sorted(_FALLBACK)}"
        )

    if embedder_name == "voyage-context-4":
        from code_indexer.services.voyage_ai import _get_voyage_model_specs

        specs = _get_voyage_model_specs()
        model_spec = specs.get("voyage_models", {}).get(embedder_name, {})
        return float(
            model_spec.get("pricing_usd_per_million_tokens", _FALLBACK[embedder_name])
        )

    from code_indexer.services.cohere_embedding import _get_cohere_model_specs

    specs = _get_cohere_model_specs()
    model_spec = specs.get("cohere_models", {}).get(embedder_name, {})
    return float(
        model_spec.get("pricing_usd_per_million_tokens", _FALLBACK[embedder_name])
    )


def _count_tokens(embedder_name: str, text: str) -> int:
    """Count tokens using the embedder's own OFFLINE tokenizer (no network)."""
    if embedder_name == "voyage-context-4":
        from code_indexer.services.embedded_voyage_tokenizer import VoyageTokenizer

        return int(VoyageTokenizer.count_tokens([text], model=embedder_name))

    from code_indexer.services.embedded_cohere_tokenizer import count_tokens_single

    return int(count_tokens_single(text, model=embedder_name))


# ---------------------------------------------------------------------------
# AC1/AC2: NEW-layout vector count -- reuses the REAL production chunker.
# ---------------------------------------------------------------------------


def compute_new_vector_count(
    doc: AggregatedCommitDocument, chunk_chars: int, overlap_percentage: float
) -> int:
    """Return the chunk/vector count for one commit's aggregated document.

    This is a thin wrapper over `chunk_aggregated_document` (the EXACT
    function production indexing calls) -- not a reimplementation -- so the
    projected count is provably identical to what real indexing produces
    (AC2's measured predicted-vs-actual check).
    """
    return len(chunk_aggregated_document(doc, chunk_chars, overlap_percentage))


# ---------------------------------------------------------------------------
# AC2: legacy per-file-diff `old_vectors` formula (documented, NOT built).
# ---------------------------------------------------------------------------


def compute_old_vectors_for_commit(
    file_changes: List[FileChange], chunk_chars: int = LEGACY_CHUNK_CHARS
) -> int:
    """Return the legacy per-file-diff vector count for one commit.

    Documented formula (Story #1292 technical details): the deleted OLD
    temporal path embedded the commit message as its OWN standalone vector,
    PLUS >=1 chunk per content-bearing changed file (chunked like a regular
    file at 15% overlap). Binary files and pure renames are skipped (the
    same _is_content_bearing rule the new aggregator uses), matching what
    the old per-file-diff scanner actually indexed.

    Reuses `FixedSizeChunker.estimate_chunks` (the exact legacy chunk-count
    formula) rather than reimplementing chunk arithmetic.
    """
    chunker = FixedSizeChunker(IndexingConfig())
    chunker.chunk_size = chunk_chars
    chunker.overlap_size = int(chunk_chars * LEGACY_OVERLAP_PERCENTAGE)
    chunker.step_size = chunker.chunk_size - chunker.overlap_size

    total = 1  # the commit message's own standalone vector
    for change in file_changes:
        if change.diff_type == "binary":
            continue
        if change.diff_type == "renamed" and not change.diff_text.strip():
            continue
        total += max(1, chunker.estimate_chunks(change.diff_text))
    return total


# ---------------------------------------------------------------------------
# AC1: git-history-only commit walk (no old-index read, no index build).
# ---------------------------------------------------------------------------


def walk_commits(
    repo_path: Path,
    max_commits: Optional[int] = None,
    since_date: Optional[str] = None,
) -> List[CommitInfo]:
    """Walk git history via `git log` ONLY -- no .code-indexer read/write.

    Mirrors TemporalIndexer._get_commit_history's parsing format but never
    touches any index/config state -- pure git-history projection (AC1).
    """
    cmd = [
        "git",
        "log",
        "--format=%H%x00%at%x00%an%x00%ae%x00%B%x00%P%x1e",
        "--reverse",
    ]
    if since_date:
        cmd.extend(["--since", since_date])
    if max_commits:
        cmd.extend(["-n", str(max_commits)])

    result = subprocess.run(
        cmd,
        cwd=repo_path,
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    )

    commits: List[CommitInfo] = []
    for record in result.stdout.strip().split("\x1e"):
        if not record.strip():
            continue
        parts = record.split("\x00")
        if len(parts) < 6:
            continue
        commits.append(
            CommitInfo(
                hash=parts[0].strip(),
                timestamp=int(parts[1]),
                author_name=parts[2],
                author_email=parts[3],
                message=parts[4].strip(),
                parent_hashes=parts[5].strip(),
            )
        )
    return commits


# ---------------------------------------------------------------------------
# Aggregate projection result
# ---------------------------------------------------------------------------


@dataclass
class EmbedderStats:
    new_vectors: int = 0
    file_count: int = 0
    estimated_tokens: int = 0
    estimated_cost_usd: float = 0.0
    overlap_percentage: float = 0.0
    pricing_usd_per_million_tokens: float = 0.0

    def to_dict(self, old_vectors: int) -> Dict[str, object]:
        ratio = (old_vectors / self.new_vectors) if self.new_vectors else 0.0
        return {
            "new_vectors": self.new_vectors,
            "file_count": self.file_count,
            "estimated_tokens": self.estimated_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "overlap_percentage": self.overlap_percentage,
            "pricing_usd_per_million_tokens": self.pricing_usd_per_million_tokens,
            "ratio_vs_old": ratio,
        }


@dataclass
class ProjectionResult:
    commit_count: int = 0
    old_vectors: int = 0
    per_embedder: Dict[str, EmbedderStats] = field(default_factory=dict)

    def ratio_for(self, embedder_name: str) -> float:
        stats = self.per_embedder[embedder_name]
        if stats.new_vectors == 0:
            return 0.0
        return self.old_vectors / stats.new_vectors


def run_projection(
    repo_path: Path,
    max_commits: Optional[int] = None,
    since_date: Optional[str] = None,
    aggregation_chunk_chars: int = 4096,
) -> ProjectionResult:
    """Walk `repo_path`'s git history once and project per-embedder counts.

    Purely git-history-derived (AC1): no .code-indexer directory is ever
    read or written; no embedding-provider network call is made (token
    counts use the embedded, offline tokenizers).
    """
    repo_path = Path(repo_path)
    commits = walk_commits(repo_path, max_commits=max_commits, since_date=since_date)

    result = ProjectionResult(commit_count=len(commits))
    for embedder_cls in _EMBEDDER_CLASSES:
        result.per_embedder[embedder_cls.name] = EmbedderStats(
            overlap_percentage=embedder_cls.overlap_percentage,
            pricing_usd_per_million_tokens=get_pricing_usd_per_million(
                embedder_cls.name
            ),
        )

    for commit in commits:
        file_changes = get_file_changes(repo_path, commit)
        doc = build_aggregated_document(commit, file_changes)

        result.old_vectors += compute_old_vectors_for_commit(file_changes)

        for embedder_cls in _EMBEDDER_CLASSES:
            stats = result.per_embedder[embedder_cls.name]
            vector_count = compute_new_vector_count(
                doc, aggregation_chunk_chars, embedder_cls.overlap_percentage
            )
            stats.new_vectors += vector_count
            stats.file_count += vector_count  # AC1: file_count == vector_count
            stats.estimated_tokens += _count_tokens(embedder_cls.name, doc.text)

    for stats in result.per_embedder.values():
        stats.estimated_cost_usd = (
            stats.estimated_tokens / 1_000_000.0
        ) * stats.pricing_usd_per_million_tokens

    return result


# ---------------------------------------------------------------------------
# AC2: measured predicted-vs-actual tolerance check (pure logic).
# ---------------------------------------------------------------------------


def within_tolerance(predicted: int, actual: int, tolerance_pct: float) -> bool:
    """Return True iff `actual` is within `tolerance_pct` of `predicted`.

    tolerance_pct is a fraction (e.g. 0.05 == 5%). predicted=0 special-cases
    to an exact-match requirement (division-by-zero guard).
    """
    if predicted == 0:
        return actual == 0
    return abs(actual - predicted) <= predicted * tolerance_pct


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def write_report(
    result: ProjectionResult, output_dir: Path, repo_label: str
) -> Path:
    """Write the projection result as JSON to `output_dir` (reports/perf/)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = output_dir / f"temporal_vector_projection_{timestamp}.json"

    data = {
        "repo": repo_label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_count": result.commit_count,
        "old_vectors": result.old_vectors,
        "old_vectors_formula": (
            "1 commit-message vector + per content-bearing changed file "
            f">=1 chunk at {LEGACY_CHUNK_CHARS} chars / "
            f"{int(LEGACY_OVERLAP_PERCENTAGE * 100)}% overlap "
            "(deleted per-file-diff layout; NOT built, computed analytically)"
        ),
        "per_embedder": {
            name: stats.to_dict(result.old_vectors)
            for name, stats in result.per_embedder.items()
        },
    }

    report_path.write_text(json.dumps(data, indent=2, sort_keys=False))
    return report_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Git-history-only temporal vector projection (Story #1292): "
            "per-embedder projected vector/file counts and token/$ cost, "
            "plus the legacy per-file-diff old_vectors count."
        )
    )
    parser.add_argument("repo", type=Path, help="Path to the git repository")
    parser.add_argument(
        "--max-commits", type=int, default=None, help="Bound the git-log walk"
    )
    parser.add_argument(
        "--since-date", type=str, default=None, help="Only commits since YYYY-MM-DD"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "reports" / "perf",
        help="Directory to write the JSON report into (default reports/perf/)",
    )
    parser.add_argument(
        "--repo-label",
        type=str,
        default=None,
        help="Human-readable repo label for the report (default: repo dir name)",
    )
    args = parser.parse_args(argv)

    repo_label = args.repo_label or args.repo.resolve().name
    result = run_projection(
        args.repo, max_commits=args.max_commits, since_date=args.since_date
    )
    report_path = write_report(result, args.output_dir, repo_label)

    print(f"Commits analyzed: {result.commit_count}")
    print(f"old_vectors (legacy per-file-diff formula): {result.old_vectors}")
    for name, stats in result.per_embedder.items():
        ratio = result.ratio_for(name)
        print(
            f"  [{name}] new_vectors={stats.new_vectors} "
            f"file_count={stats.file_count} "
            f"tokens={stats.estimated_tokens} "
            f"cost_usd={stats.estimated_cost_usd:.4f} "
            f"ratio_vs_old={ratio:.2f}x"
        )
    print(f"Report written to: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
