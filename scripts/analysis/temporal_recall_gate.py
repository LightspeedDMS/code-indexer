#!/usr/bin/env python3
"""Absolute recall-quality gate for per-commit temporal search (Story #1292 AC5).

A curated benchmark corpus of queries + known-relevant commit hashes is run
against the NEW per-commit dual-embedder index on a representative repo via
the REAL `cidx query --time-range-all` CLI front door (no mocking of search
internals). This is an ABSOLUTE gate: every documented query must surface its
expected commit within top-K after dedup-by-commit, OR the miss must be an
explicitly-accepted, documented delta. There is NO comparison to the deleted
old index -- recall is judged on the new index alone.

Usage (against an already-indexed repo):
    python3 scripts/analysis/temporal_recall_gate.py <repo> <corpus.json> \\
        [--output-dir DIR]

The corpus JSON is a list of objects:
    {"query": "...", "expected_commit_hashes": ["abc1234", ...],
     "embedder": "voyage-context-4", "top_k": 5,
     "accepted_miss": false, "note": ""}
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

# Matches "   Commit: bc1cafa (2016-09-12)" lines emitted by the CLI's
# non-quiet temporal result display (cli.py's _display_commit_message_match /
# _display_file_chunk_match).
_COMMIT_LINE_RE = re.compile(r"^\s*Commit:\s+([0-9a-f]{7,40})\b", re.MULTILINE)


def parse_commit_hashes_from_cli_output(output: str) -> List[str]:
    """Extract commit hashes from `cidx query` (non-quiet) output, IN RANK ORDER.

    Dedup-by-commit: a commit ranked more than once (multiple matching
    chunks) appears only ONCE, at its first (highest-ranked) occurrence --
    matching the search service's own dedup-by-commit behavior.
    """
    seen: Dict[str, None] = {}
    for match in _COMMIT_LINE_RE.finditer(output):
        commit_hash = match.group(1)
        if commit_hash not in seen:
            seen[commit_hash] = None
    return list(seen.keys())


def evaluate_entry(
    ranked_hashes: List[str], expected_hashes: List[str], top_k: int
) -> bool:
    """Return True iff ANY of `expected_hashes` appears within the top `top_k`
    ranked commit hashes (a query may accept more than one known-relevant
    commit, e.g. near-duplicate refinement commits)."""
    top = ranked_hashes[:top_k]
    return any(expected in top for expected in expected_hashes)


@dataclass
class CorpusEntry:
    """One documented recall-benchmark query."""

    query: str
    expected_commit_hashes: List[str]
    embedder: str
    top_k: int = 5
    accepted_miss: bool = False
    note: str = ""


@dataclass
class CorpusResult:
    entry: CorpusEntry
    ranked_hashes: List[str] = field(default_factory=list)
    hit: bool = False


def load_corpus(corpus_path: Path) -> List[CorpusEntry]:
    raw = json.loads(Path(corpus_path).read_text())
    return [
        CorpusEntry(
            query=item["query"],
            expected_commit_hashes=item["expected_commit_hashes"],
            embedder=item["embedder"],
            top_k=item.get("top_k", 5),
            accepted_miss=item.get("accepted_miss", False),
            note=item.get("note", ""),
        )
        for item in raw
    ]


def run_cli_query(
    repo_path: Path, query: str, embedder: str, limit: int
) -> str:
    """Run the REAL `cidx query --time-range-all` CLI against `repo_path`.

    Returns raw stdout (non-quiet, so per-result "Commit: <hash>" lines are
    present for parse_commit_hashes_from_cli_output).
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_indexer.cli",
            "query",
            query,
            "--time-range-all",
            "--temporal-embedder",
            embedder,
            "--limit",
            str(limit),
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def run_corpus(repo_path: Path, corpus: List[CorpusEntry]) -> List[CorpusResult]:
    """Run every corpus entry against the real front door and evaluate hits."""
    results: List[CorpusResult] = []
    for entry in corpus:
        output = run_cli_query(repo_path, entry.query, entry.embedder, entry.top_k)
        ranked = parse_commit_hashes_from_cli_output(output)
        hit = evaluate_entry(ranked, entry.expected_commit_hashes, entry.top_k)
        results.append(CorpusResult(entry=entry, ranked_hashes=ranked, hit=hit))
    return results


def write_recall_report(
    results: List[CorpusResult], output_dir: Path, repo_label: str
) -> Path:
    """Write the recall-gate results as JSON to `output_dir` (reports/perf/)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = output_dir / f"temporal_recall_gate_{timestamp}.json"

    hits = sum(1 for r in results if r.hit)
    accepted_misses = sum(
        1 for r in results if not r.hit and r.entry.accepted_miss
    )
    critical_misses = sum(
        1 for r in results if not r.hit and not r.entry.accepted_miss
    )

    data: Dict[str, Any] = {
        "repo": repo_label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gate_type": "ABSOLUTE (new index only -- no old-index comparison)",
        "total_queries": len(results),
        "hits": hits,
        "accepted_misses": accepted_misses,
        "critical_misses": critical_misses,
        "gate_passed": critical_misses == 0,
        "queries": [
            {
                "query": r.entry.query,
                "embedder": r.entry.embedder,
                "expected_commit_hashes": r.entry.expected_commit_hashes,
                "top_k": r.entry.top_k,
                "ranked_hashes": r.ranked_hashes,
                "hit": r.hit,
                "accepted_miss": r.entry.accepted_miss,
                "note": r.entry.note,
            }
            for r in results
        ],
    }

    report_path.write_text(json.dumps(data, indent=2, sort_keys=False))
    return report_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Absolute recall-quality gate for the NEW per-commit temporal "
            "index (Story #1292 AC5) -- no comparison to the old index."
        )
    )
    parser.add_argument("repo", type=Path, help="Path to the ALREADY-INDEXED repo")
    parser.add_argument("corpus", type=Path, help="Path to corpus JSON file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "reports" / "perf",
        help="Directory to write the JSON report into (default reports/perf/)",
    )
    parser.add_argument("--repo-label", type=str, default=None)
    args = parser.parse_args(argv)

    corpus = load_corpus(args.corpus)
    results = run_corpus(args.repo, corpus)
    repo_label = args.repo_label or args.repo.resolve().name
    report_path = write_recall_report(results, args.output_dir, repo_label)

    for r in results:
        status = "HIT" if r.hit else ("ACCEPTED-MISS" if r.entry.accepted_miss else "CRITICAL-MISS")
        print(f"[{status}] ({r.entry.embedder}) {r.entry.query!r} -> {r.ranked_hashes}")

    critical = sum(1 for r in results if not r.hit and not r.entry.accepted_miss)
    print(f"Report written to: {report_path}")
    print(f"Critical misses: {critical} / {len(results)}")
    return 0 if critical == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
