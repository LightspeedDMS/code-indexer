#!/usr/bin/env python3
"""Story #1493 AC3: committed, reproducible recall-comparison harness.

Reuses temporal_recall_gate.py's `parse_commit_hashes_from_cli_output`
(Story #1292's CLI-output commit-hash parser) rather than re-implementing a
second regex-based parser. Loads the fixed, recorded query set from
reports/perf/temporal_overfetch_1493_query_set.json and runs each query via
the REAL `cidx query` CLI front door against a given `--pythonpath` (so the
SAME query set can be run against a pre-#1493 checkout for the baseline
capture, and against the current checkout for the post-change capture) and
`--repo` (built by temporal_overfetch_1493_build_fixture.py).

Usage:
    # Capture one run's results:
    python3 temporal_overfetch_1493_recall_compare.py capture \\
        --repo <repo_dir> --pythonpath <src_dir> --voyage-key <key> \\
        --query-set reports/perf/temporal_overfetch_1493_query_set.json \\
        --label baseline --output reports/perf/temporal_overfetch_1493_baseline_results.json

    # Compare two captures:
    python3 temporal_overfetch_1493_recall_compare.py compare \\
        --baseline reports/perf/temporal_overfetch_1493_baseline_results.json \\
        --postchange reports/perf/temporal_overfetch_1493_postchange_results.json \\
        --output reports/perf/temporal_overfetch_1493_recall_comparison.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from temporal_recall_gate import parse_commit_hashes_from_cli_output  # noqa: E402

QUERY_SUBPROCESS_TIMEOUT_SECONDS = 120


def _run_one_query(
    repo_dir: Path, pythonpath: str, voyage_key: str, query_spec: Dict[str, Any]
) -> Dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "code_indexer.cli",
        "query",
        query_spec["query_text"],
        "--limit",
        str(query_spec["limit"]),
    ]
    if query_spec.get("time_range_all"):
        cmd.append("--time-range-all")
    elif query_spec.get("time_range"):
        cmd += ["--time-range", query_spec["time_range"]]
    if query_spec.get("chunk_type"):
        cmd += ["--chunk-type", query_spec["chunk_type"]]

    env = os.environ.copy()
    env["PYTHONPATH"] = pythonpath
    env["VOYAGE_API_KEY"] = voyage_key
    proc = subprocess.run(
        cmd,
        cwd=repo_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=QUERY_SUBPROCESS_TIMEOUT_SECONDS,
    )
    ranked_hashes = parse_commit_hashes_from_cli_output(proc.stdout)
    return {
        "query_id": query_spec["query_id"],
        "returncode": proc.returncode,
        "ranked_commit_hashes": ranked_hashes,
        "stderr_tail": proc.stderr[-500:] if proc.returncode != 0 else "",
    }


def cmd_capture(args: argparse.Namespace) -> int:
    query_set = json.loads(Path(args.query_set).read_text())
    results = []
    for query_spec in query_set["queries"]:
        result = _run_one_query(args.repo, args.pythonpath, args.voyage_key, query_spec)
        result["label"] = args.label
        results.append(result)
        print(
            f"[{args.label}] {result['query_id']}: "
            f"{len(result['ranked_commit_hashes'])} results (rc={result['returncode']})"
        )
        if result["returncode"] != 0:
            print(f"  stderr: {result['stderr_tail']}", file=sys.stderr)

    output = {
        "label": args.label,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(args.repo),
        "query_set_file": str(args.query_set),
        "results": results,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(output, indent=2))
    print(f"Capture written to: {args.output}")
    return 0 if all(r["returncode"] == 0 for r in results) else 1


def cmd_compare(args: argparse.Namespace) -> int:
    baseline = {
        r["query_id"]: r for r in json.loads(Path(args.baseline).read_text())["results"]
    }
    postchange = {
        r["query_id"]: r
        for r in json.loads(Path(args.postchange).read_text())["results"]
    }

    all_query_ids = sorted(set(baseline) | set(postchange))
    per_query: List[Dict[str, Any]] = []
    degraded = False

    for query_id in all_query_ids:
        if query_id not in baseline or query_id not in postchange:
            per_query.append({"query_id": query_id, "verdict": "MISSING_ON_ONE_SIDE"})
            degraded = True
            continue

        b_hashes = baseline[query_id]["ranked_commit_hashes"]
        p_hashes = postchange[query_id]["ranked_commit_hashes"]
        b_set, p_set = set(b_hashes), set(p_hashes)
        union = b_set | p_set
        inter = b_set & p_set
        overlap = len(inter) / len(union) if union else 1.0
        recall_vs_baseline = len(inter) / len(b_set) if b_set else 1.0

        if b_hashes == p_hashes:
            verdict = "IDENTICAL"
        elif b_set == p_set:
            verdict = "REORDERED"
        else:
            verdict = "CHANGED"
            if recall_vs_baseline < 1.0:
                degraded = True

        per_query.append(
            {
                "query_id": query_id,
                "baseline_count": len(b_hashes),
                "postchange_count": len(p_hashes),
                "overlap_jaccard": overlap,
                "recall_vs_baseline": recall_vs_baseline,
                "verdict": verdict,
                "baseline_order": b_hashes,
                "postchange_order": p_hashes,
            }
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_verdict": "FAIL" if degraded else "PASS",
        "queries": per_query,
    }
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2))
        print(f"Comparison report written to: {args.output}")

    for entry in per_query:
        print(f"[{entry['verdict']}] {entry['query_id']}")
    print(f"OVERALL: {report['overall_verdict']}")
    return 1 if degraded else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--repo", type=Path, required=True)
    capture_parser.add_argument("--pythonpath", type=str, required=True)
    capture_parser.add_argument("--voyage-key", type=str, required=True)
    capture_parser.add_argument("--query-set", type=Path, required=True)
    capture_parser.add_argument("--label", type=str, required=True)
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.set_defaults(func=cmd_capture)

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--baseline", type=Path, required=True)
    compare_parser.add_argument("--postchange", type=Path, required=True)
    compare_parser.add_argument("--output", type=Path, default=None)
    compare_parser.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
