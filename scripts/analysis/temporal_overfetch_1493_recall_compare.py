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
from typing import Any, Dict, List, Optional, Tuple

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from temporal_recall_gate import parse_commit_hashes_from_cli_output  # noqa: E402

QUERY_SUBPROCESS_TIMEOUT_SECONDS = 120

# --- Survivor-count measurement (code-review follow-up) -------------------
# The comparison above proves baseline/postchange RETURN THE SAME results,
# but does not by itself say whether that agreement is meaningful (real
# survivors comfortably exceed the requested limit) or vacuous (the corpus
# is small enough that every candidate "survives" regardless of the cap).
# `compute_survivor_counts()` measures, per query, the REAL number of
# is_head-matching candidates within the AC1-capped window vs the natural
# (uncapped) window, via a real FilesystemVectorStore.search() call against
# the already-indexed corpus -- not an estimate.


def _shard_date_range(shard_identifier: str) -> Tuple[str, int, int]:
    """Parse a shard identifier like
    'code-indexer-temporal-voyage_context_4-2024Q1' into
    (year, start_month, end_month)."""
    import re as _re

    match = _re.search(r"-(\d{4})Q([1-4])$", shard_identifier)
    if not match:
        raise ValueError(f"Cannot parse quarter suffix from shard: {shard_identifier}")
    year, quarter_num = match.group(1), int(match.group(2))
    start_month = (quarter_num - 1) * 3 + 1
    end_month = start_month + 2
    return year, start_month, end_month


def _shards_for_query(
    query_spec: Dict[str, Any], shard_identifiers: List[str]
) -> List[str]:
    """Which shard(s) a given query_spec's time filter would actually reach."""
    if query_spec.get("time_range_all"):
        return list(shard_identifiers)

    time_range = query_spec.get("time_range")
    if not time_range:
        raise ValueError(
            f"query_spec {query_spec.get('query_id')!r} has neither "
            f"time_range_all nor time_range -- cannot determine shards."
        )
    start_str, end_str = time_range.split("..")
    start_year, start_month = start_str.split("-")[0], int(start_str.split("-")[1])
    end_year, end_month = end_str.split("-")[0], int(end_str.split("-")[1])

    matching = []
    for shard in shard_identifiers:
        shard_year, shard_start, shard_end = _shard_date_range(shard)
        # Overlap check assumes both ranges fall within the SAME year (true
        # for this fixture's fixed 2024 Q1/Q3 shards).
        if (
            shard_year == start_year == end_year
            and shard_start <= end_month
            and start_month <= shard_end
        ):
            matching.append(shard)
    return matching


def _time_range_bounds(query_spec: Dict[str, Any]) -> Tuple[int, int]:
    """Mirrors temporal_search_service.py's own commit_timestamp filter
    bound computation EXACTLY (naive local-timezone datetime.timestamp(),
    end-of-day for the end bound) -- so this analysis measures the SAME
    filter window a real `cidx query` call would actually apply."""
    from datetime import datetime

    from code_indexer.services.temporal.temporal_search_service import (
        ALL_TIME_RANGE,
    )

    if query_spec.get("time_range_all"):
        start_str, end_str = ALL_TIME_RANGE
    else:
        start_str, end_str = query_spec["time_range"].split("..")

    start_ts = int(datetime.strptime(start_str, "%Y-%m-%d").timestamp())
    end_ts = int(
        datetime.strptime(end_str, "%Y-%m-%d")
        .replace(hour=23, minute=59, second=59)
        .timestamp()
    )
    return start_ts, end_ts


def _compute_search_limits(
    chunk_type: Optional[str], true_user_limit: int
) -> Tuple[int, int]:
    """Reproduces the exact natural/capped per-shard search_limit a real
    query would compute (temporal_fusion_dispatch.py's shard-level
    TEMPORAL_OVERFETCH_MULTIPLIER, then temporal_search_service.py's
    per-chunk-type multiplier, then temporal_fusion.py's
    cap_combined_overfetch_search_limit -- imported, not reimplemented, for
    the ceiling itself). Only the two small chunk-type multiplier constants
    (40x for commit_message, 1.5x for commit_diff -- see
    temporal_search_service.py's search_temporal()) are mirrored inline;
    chunk_type=None combined with a non-all-time range is out of scope (this
    fixture's query set never combines them) and raises loudly rather than
    guessing.
    """
    from code_indexer.services.temporal.temporal_fusion import (
        TEMPORAL_OVERFETCH_MULTIPLIER,
        cap_combined_overfetch_search_limit,
    )

    shard_limit = true_user_limit * TEMPORAL_OVERFETCH_MULTIPLIER
    if chunk_type == "commit_message":
        natural = shard_limit * 40
    elif chunk_type == "commit_diff":
        natural = int(shard_limit * 1.5)
    else:
        raise NotImplementedError(
            f"_compute_search_limits does not model chunk_type={chunk_type!r} "
            f"-- this fixture's query set never needs it."
        )
    capped = cap_combined_overfetch_search_limit(true_user_limit, natural)
    return natural, capped


def compute_survivor_counts(
    repo_dir: Path, pythonpath: str, voyage_key: str, query_set: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """Measure the REAL is_head-survivor count within the natural and capped
    windows for every chunk_type-filtered query, via a real
    FilesystemVectorStore.search() call (real VoyageAI query embedding,
    real HNSW, real hydrated payloads) against the already-indexed corpus.
    chunk_type=None queries are marked not-applicable -- no is_head
    post-filter runs for them, so the survivor concept does not apply.
    """
    sys.path.insert(0, pythonpath)
    os.environ["VOYAGE_API_KEY"] = voyage_key

    from code_indexer.config import VoyageAIConfig
    from code_indexer.services.voyage_ai import VoyageAIClient
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    embedder_model = "voyage-context-4"
    provider = VoyageAIClient(VoyageAIConfig(model=embedder_model))
    index_base = repo_dir / ".code-indexer" / "index"
    store = FilesystemVectorStore(base_path=index_base)

    shard_identifiers: List[str] = query_set["target_repo_fixture"]["shard_identifiers"]

    results: Dict[str, Dict[str, Any]] = {}
    for query_spec in query_set["queries"]:
        query_id = query_spec["query_id"]
        chunk_type = query_spec.get("chunk_type")

        if chunk_type is None:
            results[query_id] = {
                "applicable": False,
                "reason": "chunk_type=None performs no is_head post-filter; "
                "the AC1 cap is a documented no-op for this query.",
            }
            continue

        true_user_limit = query_spec["limit"]
        natural_limit, capped_limit = _compute_search_limits(
            chunk_type, true_user_limit
        )
        window_size = max(natural_limit, capped_limit)
        start_ts, end_ts = _time_range_bounds(query_spec)
        filter_conditions = {
            "must": [
                {"key": "commit_timestamp", "range": {"gte": start_ts, "lte": end_ts}}
            ]
        }
        shards = _shards_for_query(query_spec, shard_identifiers)

        natural_survivors = 0
        capped_survivors = 0
        per_shard: List[Dict[str, Any]] = []
        for shard in shards:
            raw_results = store.search(
                query=query_spec["query_text"],
                embedding_provider=provider,
                collection_name=shard,
                limit=window_size,
                filter_conditions=filter_conditions,
                lazy_load=True,
                prefetch_limit=max(window_size * 2, 5000),
                ef=4000,
            )
            if chunk_type == "commit_message":

                def _is_survivor(r: Dict[str, Any]) -> bool:
                    return bool(r.get("payload", {}).get("is_head"))
            else:  # commit_diff: no is_head filtering -- every candidate survives

                def _is_survivor(r: Dict[str, Any]) -> bool:
                    return True

            natural_survivors += sum(
                1 for r in raw_results[:natural_limit] if _is_survivor(r)
            )
            capped_survivors += sum(
                1 for r in raw_results[:capped_limit] if _is_survivor(r)
            )
            per_shard.append({"shard": shard, "candidates_returned": len(raw_results)})

        results[query_id] = {
            "applicable": True,
            "chunk_type": chunk_type,
            "true_user_limit": true_user_limit,
            "natural_search_limit": natural_limit,
            "capped_search_limit": capped_limit,
            "natural_window_survivors": natural_survivors,
            "capped_window_survivors": capped_survivors,
            "shards_queried": shards,
            "per_shard": per_shard,
        }

    return results


def cmd_survivors(args: argparse.Namespace) -> int:
    query_set = json.loads(Path(args.query_set).read_text())
    survivor_counts = compute_survivor_counts(
        args.repo, args.pythonpath, args.voyage_key, query_set
    )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(survivor_counts, indent=2))
    for query_id, entry in survivor_counts.items():
        if not entry["applicable"]:
            print(f"[N/A] {query_id}: {entry['reason']}")
        else:
            print(
                f"[{query_id}] capped_survivors={entry['capped_window_survivors']} "
                f"(limit={entry['capped_search_limit']}), "
                f"natural_survivors={entry['natural_window_survivors']} "
                f"(limit={entry['natural_search_limit']}), "
                f"true_user_limit={entry['true_user_limit']}"
            )
    print(f"Survivor counts written to: {args.output}")
    return 0


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
    survivor_counts: Dict[str, Dict[str, Any]] = {}
    if getattr(args, "survivor_counts", None):
        survivor_counts = json.loads(Path(args.survivor_counts).read_text())

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

        entry = {
            "query_id": query_id,
            "baseline_count": len(b_hashes),
            "postchange_count": len(p_hashes),
            "overlap_jaccard": overlap,
            "recall_vs_baseline": recall_vs_baseline,
            "verdict": verdict,
            "baseline_order": b_hashes,
            "postchange_order": p_hashes,
        }
        survivor_entry = survivor_counts.get(query_id)
        if survivor_entry is not None:
            entry["survivor_counts"] = survivor_entry
        per_query.append(entry)

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
    compare_parser.add_argument("--survivor-counts", type=Path, default=None)
    compare_parser.set_defaults(func=cmd_compare)

    survivors_parser = subparsers.add_parser(
        "survivors",
        help="Measure the real is_head-survivor count within the natural "
        "vs capped overfetch window for each chunk_type-filtered query.",
    )
    survivors_parser.add_argument("--repo", type=Path, required=True)
    survivors_parser.add_argument("--pythonpath", type=str, required=True)
    survivors_parser.add_argument("--voyage-key", type=str, required=True)
    survivors_parser.add_argument("--query-set", type=Path, required=True)
    survivors_parser.add_argument("--output", type=Path, required=True)
    survivors_parser.set_defaults(func=cmd_survivors)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
