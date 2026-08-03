#!/usr/bin/env python3
"""Story #1493 AC3: reproducible git-repo fixture builder for the temporal
overfetch-ceiling recall comparison's PRIMARY 7-query corpus.

Builds a ~520-commit, two-quarter (2024 Q1 + Q3) repo: each commit touches
one of six topic files with a short message + tiny diff. Corpus size is
chosen so the per-shard vector count (MAIN_CORPUS_COMMITS_PER_QUARTER, 260)
exceeds the AC1 combined-overfetch-ceiling's capped threshold for the
worst-case query (true_user_limit=3 * TEMPORAL_COMBINED_OVERFETCH_CEILING=60
== 180), so the cap actually truncates real candidates rather than
trivially returning "everything anyway".

The SEPARATE boundary-stress corpus (answering the code-review question
"show a case where a relevant chunk would rank between the capped and
natural overfetch window") is built and VERIFIED by
temporal_overfetch_1493_boundary_stress.py, which needs a real
build-index-measure-retry loop this pure git-repo builder does not perform.

Usage:
    python3 temporal_overfetch_1493_build_fixture.py <repo_dir>
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TOPICS = [
    ("auth", "authentication and session handling"),
    ("payment", "payment processing and billing"),
    ("search", "search indexing and ranking"),
    ("cache", "caching layer and invalidation"),
    ("logging", "structured logging and metrics"),
    ("db", "database migrations and queries"),
]
MSG_VERBS = ["fix", "add", "refactor", "optimize", "remove", "update", "improve"]

# Sizing: 260 commits/quarter so the per-shard vector count exceeds the
# capped worst-case threshold (true_user_limit=3 * CEILING=60 = 180), giving
# the cap something real to bind on for at least one query.
MAIN_CORPUS_COMMITS_PER_QUARTER = 260
QUARTER_DATE_RANGES = [("2024", 1, 3), ("2024", 7, 9)]  # (year, start_month, end_month)
DAYS_PER_MONTH_CYCLE = 27  # keeps generated day-of-month always valid (<=28)
COMMIT_HOUR_UTC = 10
DIFF_VALUE_MULTIPLIER = 2  # arbitrary but fixed content-diff constant


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


def _commit(repo_dir: Path, message: str, date_str: str) -> None:
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = date_str
    env["GIT_COMMITTER_DATE"] = date_str
    _git(repo_dir, "add", "-A", env=env)
    _git(repo_dir, "commit", "-q", "-m", message, "--date", date_str, env=env)


def build_main_corpus(repo_dir: Path) -> int:
    """Two-quarter (2024 Q1 + Q3), six-topic repo. Returns total commit count."""
    _init_repo(repo_dir)
    commit_i = 0
    for year, start_month, end_month in QUARTER_DATE_RANGES:
        for i in range(MAIN_CORPUS_COMMITS_PER_QUARTER):
            topic_key, topic_desc = TOPICS[commit_i % len(TOPICS)]
            verb = MSG_VERBS[commit_i % len(MSG_VERBS)]
            content = (
                f"# {topic_key} module\n"
                f"def {topic_key}_function_{commit_i}():\n"
                f"    '''Handles {topic_desc} (iteration {commit_i}).'''\n"
                f"    value = {commit_i}\n"
                f"    return value * {DIFF_VALUE_MULTIPLIER}\n"
            )
            _write_file(repo_dir, f"src/{topic_key}_module.py", content)
            month = start_month + (i % (end_month - start_month + 1))
            day = (i % DAYS_PER_MONTH_CYCLE) + 1
            date_str = f"{year}-{month:02d}-{day:02d}T{COMMIT_HOUR_UTC:02d}:00:00"
            message = f"{verb}: {topic_key} - {topic_desc} tweak #{commit_i}"
            _commit(repo_dir, message, date_str)
            commit_i += 1
    return commit_i


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: temporal_overfetch_1493_build_fixture.py <repo_dir>",
            file=sys.stderr,
        )
        return 1
    repo_dir = Path(sys.argv[1])
    total = build_main_corpus(repo_dir)
    print(f"main corpus built: {total} commits at {repo_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
