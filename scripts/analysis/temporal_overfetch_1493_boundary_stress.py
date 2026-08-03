#!/usr/bin/env python3
"""Story #1493 AC3: SHARDED_JSON overfetch-cap candidate-window-exclusion
demonstration (real embeddings).

Code review concern (Story #1493, this is not a hypothetical): AC2's
decode-avoidance mechanism only applies to the CHUNKS_DB storage layout --
so any recall RISK from AC1's combined-overfetch-ceiling cap lands
specifically on the legacy SHARDED_JSON layout (the layout a plain
`cidx index --index-commits` run actually produces). The primary 7-query
comparison's corpus never demonstrated the cap actually EXCLUDING a real
candidate from the raw HNSW candidate pool -- this script constructs and
VERIFIES a case that does.

Method:
  1. Build a git repo with NUM_NOISE_COMMITS commits whose messages are
     near-verbatim repeats of NOISE_MESSAGE (forcing them to rank at the
     very top of similarity for a query using that exact phrase), plus ONE
     "target" commit phrased differently (TARGET_MESSAGE) but topically
     related, so real embedding similarity should rank it BEHIND most of
     the noise commits.
  2. Index it for real (`cidx init` + `cidx index --index-commits`, real
     VoyageAI embeddings).
  3. Measure the target commit's ACTUAL raw-similarity rank by querying
     with NOISE_MESSAGE, `--time-range-all`, NO chunk_type (so
     query_temporal's "no post-filters AND all-time" branch uses
     search_limit == limit EXACTLY, no multiplier at all) and a large
     limit -- this returns commits in pure similarity-rank order,
     unaffected by AC1/AC2 entirely, so the measurement itself cannot be
     contaminated by the very mechanism it's trying to stress-test.
  4. If the measured rank does NOT fall strictly between CAPPED_THRESHOLD
     (180) and NATURAL_THRESHOLD (360) -- the window AC1's cap would
     actually exclude for the true_user_limit=3, chunk_type=commit_message
     worst case -- increase the noise-commit count and rebuild, up to
     MAX_ATTEMPTS (5, a real termination bound per this project's
     anti-unbounded-loop rule). Fails loudly (non-zero exit, explicit
     message) if the window is never hit -- this script NEVER silently
     reports a fabricated "it works" when the boundary was not actually
     exercised.
  5. On success, prints (and persists to JSON) the verified target commit
     hash, its measured rank, and the noise-commit count used.

SCOPE CORRECTION (code review, round 2): this artifact demonstrates ONLY
that AC1's cap excludes a real candidate from the raw HNSW candidate POOL
(rank 301 falls outside the capped window of 180 but inside the natural
window of 360). It does NOT, by itself, demonstrate a concrete recall LOSS
at the report's documented worst case (chunk_type=commit_message,
true_user_limit=3): with a display limit of 3, a candidate ranked 301st can
never appear in a top-3 BY-SCORE result regardless of whether the raw
candidate pool includes it or not -- there are 300 higher-scoring
candidates competing for the same 3 display slots either way. A follow-up
step demonstrating an ACTUAL baseline-vs-postchange recall difference at
chunk_type=commit_message/limit=3 against this SAME repo was considered and
found impractical: producing a genuine real-embedding scenario where (a) a
target's raw rank falls strictly in the (180, 360] excluded window AND (b)
that same target would otherwise legitimately place in the final top-3 by
score requires engineering a specific mix of head/non-head chunk ranks that
real embedding geometry does not reliably reproduce on demand. That exact
scenario IS demonstrated -- deterministically, reproducibly, with a real
FilesystemVectorStore.search() call -- by the engineered-vector unit test
`tests/unit/storage/test_temporal_overfetch_1493_sharded_json_boundary_risk.py`,
which is the genuine, review-approved recall-loss proof. This script's
JSON output should be read as a "candidate-window exclusion demonstration"
(real embeddings prove the cap really does exclude a real candidate from
consideration), not as a recall-loss demonstration.

Usage:
    python3 temporal_overfetch_1493_boundary_stress.py \\
        <repo_dir> <pythonpath> <voyage_api_key> [--output result.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

NOISE_MESSAGE = (
    "refactor authentication module for better security token validation flow"
)
TARGET_MESSAGE = (
    "renew user session credentials periodically to prevent premature expiry issues"
)
TARGET_FILE_CONTENT = (
    "# session_renewal module\n"
    "def renew_session_credentials():\n"
    "    '''Periodically renews session credentials before they expire.'''\n"
    "    return True\n"
)

# The exact window this story's AC1 cap creates for the documented worst
# case (true_user_limit=3, chunk_type=commit_message): shard_multiplier=3 *
# chunk_type_multiplier=40 -> natural=360; TEMPORAL_COMBINED_OVERFETCH_
# CEILING=60 -> capped=3*60=180. A candidate ranked strictly between these
# two values is exactly the "would be excluded by the cap, would have been
# included without it" case the code review asked to see exercised.
CAPPED_THRESHOLD = 180
NATURAL_THRESHOLD = 360
# The documented worst case's true_user_limit == the final display limit
# (chunk_type=commit_message, true_user_limit=3) -- used only to explain WHY
# a rank this far outside top-K can never surface regardless of the cap.
DISPLAY_LIMIT_DOCUMENTED_WORST_CASE = 3

INITIAL_NOISE_COMMIT_COUNT = 300
NOISE_COMMIT_COUNT_INCREMENT = 100
MAX_ATTEMPTS = 5
RAW_RANK_QUERY_LIMIT = 500  # > NATURAL_THRESHOLD, so the raw ranking is complete
COMMIT_DAY_CYCLE = 27
QUARTER_YEAR = "2024"
QUARTER_MONTH = 1
TARGET_COMMIT_DAY = 15

_COMMIT_LINE_RE = re.compile(r"^\s*Commit:\s+([0-9a-f]{7,40})\b", re.MULTILINE)


def _git(repo_dir: Path, *args: str, env: dict) -> None:
    subprocess.run(["git", *args], cwd=repo_dir, env=env, check=True)


def _build_repo(repo_dir: Path, noise_commit_count: int) -> str:
    """Build the noise+target repo fresh (removing any prior attempt's
    directory first). Returns the target commit's full hash."""
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "dev@example.com"], cwd=repo_dir, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Dev Tester"], cwd=repo_dir, check=True
    )

    for i in range(noise_commit_count):
        content = (
            "# auth module\n"
            f"def auth_noise_function_{i}():\n"
            f"    '''{NOISE_MESSAGE} (variant {i}).'''\n"
            f"    return {i}\n"
        )
        (repo_dir / "src").mkdir(exist_ok=True)
        (repo_dir / "src" / "auth_module.py").write_text(content)
        day = (i % COMMIT_DAY_CYCLE) + 1
        date_str = f"{QUARTER_YEAR}-{QUARTER_MONTH:02d}-{day:02d}T09:00:00"
        message = f"{NOISE_MESSAGE} (variant {i})"
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = date_str
        env["GIT_COMMITTER_DATE"] = date_str
        _git(repo_dir, "add", "-A", env=env)
        _git(repo_dir, "commit", "-q", "-m", message, "--date", date_str, env=env)

    (repo_dir / "src").mkdir(exist_ok=True)
    (repo_dir / "src" / "session_renewal.py").write_text(TARGET_FILE_CONTENT)
    target_date_str = (
        f"{QUARTER_YEAR}-{QUARTER_MONTH:02d}-{TARGET_COMMIT_DAY:02d}T12:00:00"
    )
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = target_date_str
    env["GIT_COMMITTER_DATE"] = target_date_str
    _git(repo_dir, "add", "-A", env=env)
    _git(
        repo_dir,
        "commit",
        "-q",
        "-m",
        TARGET_MESSAGE,
        "--date",
        target_date_str,
        env=env,
    )
    target_hash = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return target_hash


def _index_repo(repo_dir: Path, pythonpath: str, voyage_key: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = pythonpath
    env["VOYAGE_API_KEY"] = voyage_key
    subprocess.run(
        [
            sys.executable,
            "-m",
            "code_indexer.cli",
            "init",
            "--embedding-provider",
            "voyage-ai",
        ],
        cwd=repo_dir,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [sys.executable, "-m", "code_indexer.cli", "index"],
        cwd=repo_dir,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    subprocess.run(
        [sys.executable, "-m", "code_indexer.cli", "index", "--index-commits"],
        cwd=repo_dir,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _measure_target_rank(
    repo_dir: Path, pythonpath: str, voyage_key: str, target_hash: str
) -> Optional[int]:
    """Query with NOISE_MESSAGE, no chunk_type, --time-range-all, a large
    limit -- returns commits in PURE raw-similarity order (query_temporal's
    "no post-filters and all-time" branch uses search_limit == limit
    exactly, entirely unaffected by AC1/AC2). Returns the target's 1-based
    rank, or None if it does not appear in the returned window at all."""
    env = os.environ.copy()
    env["PYTHONPATH"] = pythonpath
    env["VOYAGE_API_KEY"] = voyage_key
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "code_indexer.cli",
            "query",
            NOISE_MESSAGE,
            "--time-range-all",
            "--limit",
            str(RAW_RANK_QUERY_LIMIT),
        ],
        cwd=repo_dir,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    ranked_hashes = _dedup_preserve_order(_COMMIT_LINE_RE.findall(proc.stdout))
    for idx, commit_hash in enumerate(ranked_hashes, start=1):
        if target_hash.startswith(commit_hash) or commit_hash.startswith(
            target_hash[: len(commit_hash)]
        ):
            return idx
    return None


def _dedup_preserve_order(items: List[str]) -> List[str]:
    seen: dict = {}
    for item in items:
        if item not in seen:
            seen[item] = None
    return list(seen.keys())


def find_and_verify_boundary_case(
    repo_dir: Path, pythonpath: str, voyage_key: str
) -> dict:
    """Bounded build-index-measure-retry loop. Raises RuntimeError (loud
    failure, never a silent unverified success) if MAX_ATTEMPTS is
    exhausted without landing the target's rank in the capped..natural
    window."""
    noise_count = INITIAL_NOISE_COMMIT_COUNT
    attempts_log = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        target_hash = _build_repo(repo_dir, noise_count)
        _index_repo(repo_dir, pythonpath, voyage_key)
        rank = _measure_target_rank(repo_dir, pythonpath, voyage_key, target_hash)
        attempts_log.append(
            {
                "attempt": attempt,
                "noise_commit_count": noise_count,
                "measured_rank": rank,
            }
        )

        if rank is not None and CAPPED_THRESHOLD < rank <= NATURAL_THRESHOLD:
            return {
                "success": True,
                "target_commit_hash": target_hash,
                "measured_rank": rank,
                "noise_commit_count": noise_count,
                "capped_threshold": CAPPED_THRESHOLD,
                "natural_threshold": NATURAL_THRESHOLD,
                "attempts": attempts_log,
                "characterization": "candidate_window_exclusion",
                "display_limit": DISPLAY_LIMIT_DOCUMENTED_WORST_CASE,
                "does_not_demonstrate": (
                    "concrete recall loss -- with display_limit="
                    f"{DISPLAY_LIMIT_DOCUMENTED_WORST_CASE}, a candidate ranked "
                    f"{rank} can never appear in a top-"
                    f"{DISPLAY_LIMIT_DOCUMENTED_WORST_CASE} by-score result "
                    "regardless of whether the raw candidate pool includes it; "
                    "the genuine recall-loss proof is the deterministic "
                    "engineered-vector unit test "
                    "tests/unit/storage/"
                    "test_temporal_overfetch_1493_sharded_json_boundary_risk.py"
                ),
            }

        # rank too low (target ranked too high in similarity, below the
        # capped threshold) -> need MORE noise ahead of it. rank None or
        # beyond natural -> already past the window on the high side;
        # additional noise only pushes it further away, but we still bound
        # retries rather than looping forever -- if this direction is
        # wrong the loud failure below documents it honestly.
        noise_count += NOISE_COMMIT_COUNT_INCREMENT

    raise RuntimeError(
        f"Could not land the target commit's rank in the "
        f"({CAPPED_THRESHOLD}, {NATURAL_THRESHOLD}] window after "
        f"{MAX_ATTEMPTS} attempts. Attempts: {attempts_log}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_dir", type=Path)
    parser.add_argument("pythonpath", type=str)
    parser.add_argument("voyage_api_key", type=str)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    result = find_and_verify_boundary_case(
        args.repo_dir, args.pythonpath, args.voyage_api_key
    )
    print(json.dumps(result, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
