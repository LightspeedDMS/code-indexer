"""Story #1292 AC3: broadened stale-reference grep gate for temporal docs.

Verifies CLAUDE.md and docs/**/*.md describe the per-commit dual-embedder
temporal model with NO stale references to the deleted per-file-diff layout:
  - removed symbol names (_process_commits_parallel, _index_commit_message,
    temporal_migration_service and related conversion machinery)
  - ":diff:" point ids
  - standalone commit-message vectors (as a distinct vector type)
  - monolith->shard conversion/migration language
  - temporal env-var configuration
  - per-file-diff layout claims (single unsharded code-indexer-temporal/ dir)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

_PROJECT_ROOT = Path(__file__).parents[3]
_CLAUDE_MD = _PROJECT_ROOT / "CLAUDE.md"
_DOCS_DIR = _PROJECT_ROOT / "docs"

# Each pattern is a compiled regex; matching text is reported with the file
# and line number so a genuine violation is easy to locate.
_STALE_PATTERNS: Dict[str, re.Pattern] = {
    "_process_commits_parallel": re.compile(r"_process_commits_parallel"),
    "_index_commit_message": re.compile(r"_index_commit_message"),
    "temporal_migration_service": re.compile(r"temporal_migration_service"),
    ":diff: point ids": re.compile(r":diff:"),
    "standalone commit-message vector language": re.compile(
        r"standalone\s+commit[- ]message\s+vectors?", re.IGNORECASE
    ),
    "monolith->shard conversion language": re.compile(
        r"monolith\w*.{0,40}?shards?", re.IGNORECASE | re.DOTALL
    ),
    "temporal env-var configuration": re.compile(
        r"CIDX_TEMPORAL_[A-Z_]+", re.IGNORECASE
    ),
    "per-file-diff unsharded layout claim": re.compile(
        r"code-indexer-temporal/(?!\{)", re.IGNORECASE
    ),
}


def _relevant_markdown_files() -> List[Path]:
    files = [_CLAUDE_MD]
    files.extend(sorted(_DOCS_DIR.rglob("*.md")))
    return [f for f in files if f.exists()]


def test_no_stale_temporal_references_in_docs() -> None:
    violations: List[str] = []
    for md_file in _relevant_markdown_files():
        text = md_file.read_text(encoding="utf-8", errors="replace")
        for label, pattern in _STALE_PATTERNS.items():
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                violations.append(
                    f"{md_file.relative_to(_PROJECT_ROOT)}:{line_no}: "
                    f"stale reference [{label}]: {match.group(0)!r}"
                )

    assert not violations, "Stale temporal doc references found:\n" + "\n".join(
        violations
    )


def test_grep_gate_covers_every_documented_stale_category() -> None:
    """Regression guard on the gate itself: all 8 categories from the AC3
    acceptance criteria must be represented as patterns."""
    expected_categories = {
        "_process_commits_parallel",
        "_index_commit_message",
        "temporal_migration_service",
        ":diff: point ids",
        "standalone commit-message vector language",
        "monolith->shard conversion language",
        "temporal env-var configuration",
        "per-file-diff unsharded layout claim",
    }
    assert set(_STALE_PATTERNS.keys()) == expected_categories


def test_gate_actually_detects_a_planted_violation(tmp_path: Path) -> None:
    """Positive control: the gate must actually FIRE on each banned pattern,
    not just pass vacuously because the patterns never match anything."""
    for label, pattern in _STALE_PATTERNS.items():
        sample_text = {
            "_process_commits_parallel": "calls _process_commits_parallel internally",
            "_index_commit_message": "see _index_commit_message for details",
            "temporal_migration_service": "uses temporal_migration_service to convert",
            ":diff: point ids": "point id format is {project}:diff:{hash}:{n}",
            "standalone commit-message vector language": (
                "creates standalone commit-message vectors for each commit"
            ),
            "monolith->shard conversion language": (
                "background migration of monolithic indexes to quarterly shards"
            ),
            "temporal env-var configuration": "set CIDX_TEMPORAL_CHUNK_SIZE=4096",
            "per-file-diff unsharded layout claim": (
                "stored in .code-indexer/index/code-indexer-temporal/meta.json"
            ),
        }[label]
        assert pattern.search(sample_text), (
            f"pattern for {label!r} failed to match its own planted sample"
        )
