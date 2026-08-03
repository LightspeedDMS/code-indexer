#!/usr/bin/env python3
"""Story #1493 AC3: reproducible git-repo fixture builder for the temporal
overfetch-ceiling recall comparison's PRIMARY 7-query corpus.

Code-review methodology fix: the ORIGINAL version of this script wrote a
~491-character aggregated document (short message + tiny diff) per commit.
Since TemporalConfig.aggregation_chunk_chars defaults to 4096 and chunking is
`ceil(len(text) / chunk_chars)` with 0% overlap (contextual_chunker.py, the
active voyage-context-4 embedder's overlap_percentage=0.0), every commit in
that corpus produced EXACTLY ONE chunk (chunk_index=0, always "head") --
100% head chunks, 0% non-head. That made the recall-comparison test
structurally incapable of detecting a regression: AC1's overfetch cap can
never truncate away a needed result when there is nothing but head chunks to
begin with, and AC2's decode-skip-for-non-head-chunks logic never fires when
there are no non-head chunks to skip.

Fixed by making each commit's diff large enough that its aggregated document
(message + diff) chunks into ~TARGET_CHUNKS_PER_COMMIT (36) pieces -- the
same ~2.7% head-chunk density
temporal_overfetch_1493_ac4_benchmark.py already uses (30 commits x 36
chunks/commit). Each commit fully replaces its topic file's content with
NUM_CONTENT_LINES freshly-generated, mutually-unique lines (baking the
commit index into every line guarantees zero overlap with the previous
commit's content, so `git diff` shows a full remove+add with no matching
context lines -- a deterministic, precisely-sizeable diff). The FIRST commit
that ever touches a given topic file is a pure file addition (only "+"
lines, no "-" lines) and so produces roughly half the diff size (~18 chunks
instead of ~36) -- this is measured and reported honestly rather than
disguised; see `compute_achieved_corpus_stats()` and the `--output` JSON
artifact.

After building, this script computes the ACTUAL per-commit chunk count via
the REAL production `commit_aggregator`/`contextual_chunker` code (the same
functions `TemporalIndexer` calls during real indexing) -- not an estimate --
and asserts the achieved head-chunk ratio is small (bounded well below the
1-chunk-per-commit bug's 100%), so a future reader can verify the corpus is
realistic without re-deriving the character-count math themselves.

Usage:
    python3 temporal_overfetch_1493_build_fixture.py <repo_dir> <pythonpath> \\
        [--output result.json]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

TOPICS = [
    ("auth", "authentication and session handling"),
    ("payment", "payment processing and billing"),
    ("search", "search indexing and ranking"),
    ("cache", "caching layer and invalidation"),
    ("logging", "structured logging and metrics"),
    ("db", "database migrations and queries"),
]
MSG_VERBS = ["fix", "add", "refactor", "optimize", "remove", "update", "improve"]

# Sizing: 10 commits/quarter. Q1 is the constrained shard -- its first 6
# commits are each a NEW topic file (pure addition, ~half-size diff, ~18
# chunks each: see module docstring), so Q1's real total is
# 6*18 + (10-6)*36 = 252 chunks, not the naive 10*36 = 360 a uniform
# estimate would suggest. 252 still comfortably exceeds the AC1
# combined-overfetch-ceiling's capped threshold for the worst-case query
# (true_user_limit=3 * TEMPORAL_COMBINED_OVERFETCH_CEILING=60 = 180) -- so
# the cap genuinely truncates real per-shard candidates -- while keeping the
# real-embedding volume (real VoyageAI calls, real money/time) bounded.
# Commit COUNT no longer needs to be large: realistic PER-COMMIT chunking
# (not commit count) is what makes the corpus realistic.
MAIN_CORPUS_COMMITS_PER_QUARTER = 10
QUARTER_DATE_RANGES = [("2024", 1, 3), ("2024", 7, 9)]  # (year, start_month, end_month)
DAYS_PER_MONTH_CYCLE = 27  # keeps generated day-of-month always valid (<=28)
COMMIT_HOUR_UTC = 10

# --- Per-commit content sizing -------------------------------------------
# Each commit fully replaces its topic file's content with NUM_CONTENT_LINES
# fixed-width lines. A "modified" commit (file already existed) diffs as
# NUM_CONTENT_LINES removed + NUM_CONTENT_LINES added lines; a pure-addition
# commit (file's first appearance) diffs as NUM_CONTENT_LINES added lines
# only (~half the diff size, ~half the chunk count -- reported, not hidden).
CHUNK_CHARS = 4096  # must match TemporalConfig.aggregation_chunk_chars default
TARGET_CHUNKS_PER_COMMIT = 36  # matches the AC4 benchmark's 2.7% head-density ratio
LINE_PAYLOAD_CHARS = 70  # fixed-width payload per synthetic diff line
_LINE_PREFIX_CHARS = 13  # len(f"{commit_i:05d}_{line_num:06d}_")
HUNK_HEADER_APPROX_CHARS = 30  # approx length of a unified-diff "@@ ... @@" line
NON_DIFF_OVERHEAD_CHARS_BUDGET = (
    140  # slack reserved for commit message + file-path header
)

# Aim for the MIDDLE of the (TARGET-1, TARGET] chunk-count bracket so normal
# message/path-length variance never crosses a chunk-count boundary.
_TARGET_AGGREGATED_TEXT_CHARS = (
    TARGET_CHUNKS_PER_COMMIT - 1
) * CHUNK_CHARS + CHUNK_CHARS // 2
NUM_CONTENT_LINES = round(
    (
        _TARGET_AGGREGATED_TEXT_CHARS
        - NON_DIFF_OVERHEAD_CHARS_BUDGET
        - HUNK_HEADER_APPROX_CHARS
    )
    / (2 * (LINE_PAYLOAD_CHARS + 2))
)

# AC1's combined-overfetch-ceiling cap for the report's documented worst case
# (true_user_limit=3, chunk_type=commit_message): shard_multiplier(3) *
# TEMPORAL_COMBINED_OVERFETCH_CEILING(60) = 180. Each quarter's real chunk
# total must exceed this for the cap to genuinely truncate candidates.
WORST_CASE_CAPPED_THRESHOLD = 180

# Loud regression guard: the ORIGINAL bug produced a 100% head-chunk ratio
# (1 chunk per commit, always the head). A properly-sized corpus must stay
# far below that -- this bounds it well under the realistic ~2.7-4% range
# this design targets, while tolerating first-touch-commit variance.
MAX_ACCEPTABLE_ACHIEVED_HEAD_RATIO = 0.10


def _git(repo_dir: Path, *args: str, env: dict) -> None:
    subprocess.run(["git", *args], cwd=repo_dir, env=env, check=True)


def _init_repo(repo_dir: Path) -> None:
    """Create repo_dir and `git init` it. Fails loudly if repo_dir already
    exists and is non-empty -- this is a deterministic fixture builder, not
    a merge/append tool, and silently building on top of stale content
    would make the resulting corpus non-reproducible."""
    if repo_dir.exists() and any(repo_dir.iterdir()):
        raise FileExistsError(
            f"repo_dir {repo_dir} already exists and is non-empty -- "
            f"refusing to build on top of stale content. Remove it first."
        )
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "dev@example.com"], cwd=repo_dir, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Dev Tester"], cwd=repo_dir, check=True
    )


def _write_file(repo_dir: Path, relative_path: str, content: str) -> None:
    full_path = repo_dir / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)


def _make_line(commit_i: int, line_num: int) -> str:
    """A fixed-width (LINE_PAYLOAD_CHARS) line whose content bakes in both
    the commit index and the line number, guaranteeing it never matches any
    line from any other commit's content (so `git diff` always shows a full
    remove+add for a modified file, with no accidental context matches)."""
    prefix = f"{commit_i:05d}_{line_num:06d}_"
    if len(prefix) != _LINE_PREFIX_CHARS:
        raise ValueError(
            f"line prefix length drifted: expected {_LINE_PREFIX_CHARS}, "
            f"got {len(prefix)} for prefix {prefix!r}"
        )
    pad_len = LINE_PAYLOAD_CHARS - len(prefix)
    if pad_len < 0:
        raise ValueError(
            f"LINE_PAYLOAD_CHARS ({LINE_PAYLOAD_CHARS}) too small for "
            f"prefix length ({len(prefix)})"
        )
    return prefix + ("x" * pad_len)


def _topic_file_content(commit_i: int) -> str:
    lines = [_make_line(commit_i, n) for n in range(NUM_CONTENT_LINES)]
    return "\n".join(lines) + "\n"


def _commit(repo_dir: Path, message: str, date_str: str) -> str:
    """Commit staged changes and return the new commit's full hash."""
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = date_str
    env["GIT_COMMITTER_DATE"] = date_str
    _git(repo_dir, "add", "-A", env=env)
    _git(repo_dir, "commit", "-q", "-m", message, "--date", date_str, env=env)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def build_main_corpus(repo_dir: Path) -> int:
    """Two-quarter (2024 Q1 + Q3), six-topic repo. Returns total commit count."""
    _init_repo(repo_dir)
    commit_i = 0
    for year, start_month, end_month in QUARTER_DATE_RANGES:
        for i in range(MAIN_CORPUS_COMMITS_PER_QUARTER):
            topic_key, topic_desc = TOPICS[commit_i % len(TOPICS)]
            verb = MSG_VERBS[commit_i % len(MSG_VERBS)]
            content = _topic_file_content(commit_i)
            _write_file(repo_dir, f"src/{topic_key}_module.py", content)
            month = start_month + (i % (end_month - start_month + 1))
            day = (i % DAYS_PER_MONTH_CYCLE) + 1
            date_str = f"{year}-{month:02d}-{day:02d}T{COMMIT_HOUR_UTC:02d}:00:00"
            message = f"{verb}: {topic_key} - {topic_desc} tweak #{commit_i}"
            _commit(repo_dir, message, date_str)
            commit_i += 1
    return commit_i


def _quarter_label_for_timestamp(ts: int) -> Optional[str]:
    """Map a commit's UNIX timestamp to its shard quarter label
    ("2024Q1"/"2024Q3"), or None if outside both target quarters."""
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    for year, start_month, end_month in QUARTER_DATE_RANGES:
        if str(dt.year) == year and start_month <= dt.month <= end_month:
            quarter_num = (start_month - 1) // 3 + 1
            return f"{year}Q{quarter_num}"
    return None


def compute_achieved_corpus_stats(repo_dir: Path, pythonpath: str) -> Dict[str, Any]:
    """Walk every commit in `repo_dir` and compute its REAL chunk count via
    the actual production commit_aggregator/contextual_chunker code (the
    same functions TemporalIndexer calls during real indexing) -- never an
    estimate. Returns a JSON-serializable stats dict."""
    sys.path.insert(0, pythonpath)
    from code_indexer.services.temporal.commit_aggregator import (
        build_aggregated_document,
        get_file_changes,
    )
    from code_indexer.services.temporal.contextual_chunker import (
        chunk_aggregated_document,
    )
    from code_indexer.services.temporal.models import CommitInfo

    log_format = "%H%x01%P%x01%at%x01%an%x01%ae%x01%s"
    result = subprocess.run(
        ["git", "log", "--reverse", f"--format={log_format}"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )

    per_commit: List[Dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        commit_hash, parents, ts_str, author_name, author_email, subject = line.split(
            "\x01"
        )
        commit = CommitInfo(
            hash=commit_hash,
            timestamp=int(ts_str),
            author_name=author_name,
            author_email=author_email,
            message=subject,
            parent_hashes=parents,
        )
        file_changes = get_file_changes(repo_dir, commit)
        doc = build_aggregated_document(commit, file_changes)
        chunks = chunk_aggregated_document(
            doc, chunk_chars=CHUNK_CHARS, overlap_percentage=0.0
        )
        per_commit.append(
            {
                "hash": commit_hash[:7],
                "timestamp": int(ts_str),
                "quarter": _quarter_label_for_timestamp(int(ts_str)),
                "chunk_count": len(chunks),
                "head_chunk_count": sum(1 for c in chunks if c.is_head),
            }
        )

    total_commits = len(per_commit)
    total_chunks = sum(c["chunk_count"] for c in per_commit)
    total_head_chunks = sum(c["head_chunk_count"] for c in per_commit)
    achieved_head_ratio = total_head_chunks / total_chunks if total_chunks else 1.0

    per_quarter: Dict[str, Dict[str, int]] = {}
    for c in per_commit:
        label = c["quarter"] or "OUTSIDE_TARGET_QUARTERS"
        bucket = per_quarter.setdefault(label, {"commits": 0, "chunks": 0})
        bucket["commits"] += 1
        bucket["chunks"] += c["chunk_count"]

    return {
        "chunk_chars": CHUNK_CHARS,
        "target_chunks_per_commit": TARGET_CHUNKS_PER_COMMIT,
        "worst_case_capped_threshold": WORST_CASE_CAPPED_THRESHOLD,
        "total_commits": total_commits,
        "total_chunks": total_chunks,
        "total_head_chunks": total_head_chunks,
        "achieved_head_ratio": achieved_head_ratio,
        "max_acceptable_achieved_head_ratio": MAX_ACCEPTABLE_ACHIEVED_HEAD_RATIO,
        "chunk_count_min": min((c["chunk_count"] for c in per_commit), default=0),
        "chunk_count_max": max((c["chunk_count"] for c in per_commit), default=0),
        "per_quarter": per_quarter,
        "per_commit": per_commit,
    }


def _assert_corpus_is_realistic(stats: Dict[str, Any]) -> None:
    """Loud, fail-fast guards against the exact bug this fixture exists to
    avoid reproducing (see module docstring)."""
    if stats["total_chunks"] <= stats["total_commits"]:
        raise AssertionError(
            f"Corpus produced <= 1 chunk/commit on average "
            f"(total_chunks={stats['total_chunks']}, "
            f"total_commits={stats['total_commits']}) -- this is exactly "
            f"the original bug (100% head chunks, 0% non-head)."
        )
    if stats["achieved_head_ratio"] > MAX_ACCEPTABLE_ACHIEVED_HEAD_RATIO:
        raise AssertionError(
            f"achieved_head_ratio ({stats['achieved_head_ratio']:.4f}) "
            f"exceeds MAX_ACCEPTABLE_ACHIEVED_HEAD_RATIO "
            f"({MAX_ACCEPTABLE_ACHIEVED_HEAD_RATIO}) -- corpus is not "
            f"realistically non-head-dominated."
        )
    for label, bucket in stats["per_quarter"].items():
        if label == "OUTSIDE_TARGET_QUARTERS":
            continue
        if bucket["chunks"] <= WORST_CASE_CAPPED_THRESHOLD:
            raise AssertionError(
                f"Quarter {label} has only {bucket['chunks']} chunks, which "
                f"does not exceed WORST_CASE_CAPPED_THRESHOLD "
                f"({WORST_CASE_CAPPED_THRESHOLD}) -- the AC1 cap would not "
                f"genuinely truncate real candidates for this shard."
            )


def _validate_output_path(output: Path) -> None:
    if output.suffix != ".json":
        raise SystemExit(f"--output must end in .json, got: {output}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_dir", type=Path)
    parser.add_argument(
        "pythonpath",
        type=str,
        help="src/ directory to import code_indexer's commit_aggregator/"
        "contextual_chunker from, for computing the ACHIEVED chunk stats.",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    total = build_main_corpus(args.repo_dir)
    print(f"main corpus built: {total} commits at {args.repo_dir}")

    stats = compute_achieved_corpus_stats(args.repo_dir, args.pythonpath)
    _assert_corpus_is_realistic(stats)

    print(
        f"achieved: {stats['total_commits']} commits, "
        f"{stats['total_chunks']} chunks, "
        f"head_ratio={stats['achieved_head_ratio']:.4f} "
        f"(chunk_count range [{stats['chunk_count_min']}, "
        f"{stats['chunk_count_max']}])"
    )
    for label, bucket in sorted(stats["per_quarter"].items()):
        print(f"  {label}: {bucket['commits']} commits, {bucket['chunks']} chunks")

    if args.output:
        _validate_output_path(args.output)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(stats, indent=2))
        print(f"Corpus stats written to: {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
