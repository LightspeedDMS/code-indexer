#!/usr/bin/env python3
"""
Multi-Worker Throughput Benchmark for CIDX Server (Story #1168).

Measures search throughput across worker counts under 4 scenarios:
  - repeating + cache-on   (same query repeated, embedding cache enabled)
  - repeating + cache-off  (same query repeated, cache bypassed)
  - unique    + cache-on   (distinct queries each time, cache enabled)
  - unique    + cache-off  (distinct queries each time, cache bypassed)

IMPORTANT - Server Management Policy
-------------------------------------
This script does NOT auto-start or auto-stop any server process. The operator
is responsible for running one or more cidx-server instances (e.g. via
``uvicorn code_indexer.server.app:app --workers N``) and pointing this script
at each instance:

  python3 scripts/analysis/multi_worker_throughput.py \\
    --server http://localhost:8001 \\
    --workers 1 \\
    --queries 200 \\
    --concurrency 20

To test 1/2/3/4 workers the operator restarts the server between runs and
re-invokes the script, OR provides all counts in a single ``--workers 1,2,3,4``
run while the server is already running with the appropriate configuration.

Verified Endpoint Paths
-----------------------
- POST /auth/login         -- bearer token login
- POST /api/query          -- semantic search (query_text, no_embedding_cache_shortcut)
- GET  /health             -- server readiness probe
- GET  /api/admin/coalescer-metrics -- provider embed call counters (provider_embed_calls)
- GET  /cache/stats        -- HNSW index cache stats (hit_count / miss_count)

Embedding-cache stats (hit%): There is no dedicated JSON REST endpoint for the
query-embedding cache in the current server. Cache effectiveness is approximated
via provider_embed_calls from /api/admin/coalescer-metrics: fewer provider calls
relative to total requests indicates higher cache utilisation.

Usage
-----
  python3 scripts/analysis/multi_worker_throughput.py --help
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("benchmark")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_REPEATING_QUERY = "authentication function"
_FIXTURES_PATH = (
    Path(__file__).parent.parent.parent
    / "tests"
    / "performance"
    / "fixtures"
    / "benchmark_queries.txt"
)
_REPORTS_DIR = Path(__file__).parent.parent.parent / "reports" / "perf"
_LOGIN_TIMEOUT = 15.0
_QUERY_TIMEOUT = 30.0
_HEALTH_TIMEOUT = 10.0
_METRICS_TIMEOUT = 10.0

_FALLBACK_QUERIES = [
    "authentication function",
    "error handling",
    "database connection",
    "API endpoint",
    "cache invalidation",
    "user login",
    "password validation",
    "token refresh",
    "session management",
    "rate limiting",
]


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _load_credentials(local_testing_path: str = ".local-testing") -> Tuple[str, str]:
    """
    Load admin credentials from environment or .local-testing file.

    Priority:
      1. E2E_ADMIN_USER / E2E_ADMIN_PASS environment variables
      2. E2E_ADMIN_USERNAME / E2E_ADMIN_PASSWORD environment variables
      3. KEY=VALUE pairs in the .local-testing file
         (supports both E2E_ADMIN_USER/E2E_ADMIN_PASS and
          E2E_ADMIN_USERNAME/E2E_ADMIN_PASSWORD naming conventions)

    Returns:
        (username, password) tuple

    Raises:
        SystemExit: if credentials are not found
    """
    # Try short-form env vars first (e2e-automation.sh convention)
    username = os.environ.get("E2E_ADMIN_USER", "")
    password = os.environ.get("E2E_ADMIN_PASS", "")

    # Fall back to long-form env vars (.local-testing convention)
    if not username:
        username = os.environ.get("E2E_ADMIN_USERNAME", "")
    if not password:
        password = os.environ.get("E2E_ADMIN_PASSWORD", "")

    if not username or not password:
        lt_path = Path(local_testing_path)
        if not lt_path.exists():
            lt_path = Path(__file__).parent.parent.parent / ".local-testing"

        if lt_path.exists():
            for line in lt_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # Support both naming conventions
                if key in ("E2E_ADMIN_USER", "E2E_ADMIN_USERNAME") and not username:
                    username = value
                elif key in ("E2E_ADMIN_PASS", "E2E_ADMIN_PASSWORD") and not password:
                    password = value

    if not username or not password:
        log.error(
            "Admin credentials not found. "
            "Set E2E_ADMIN_USER / E2E_ADMIN_PASS (or E2E_ADMIN_USERNAME / "
            "E2E_ADMIN_PASSWORD) environment variables, or add them to .local-testing."
        )
        sys.exit(1)

    return username, password


# ---------------------------------------------------------------------------
# Server health probe
# ---------------------------------------------------------------------------


async def _wait_for_health(
    server: str, token: Optional[str] = None, timeout: float = 30.0
) -> bool:
    """
    Return True when the server's /health endpoint responds 200.

    The /health endpoint requires authentication (Depends(get_current_user)),
    so a bearer token MUST be supplied for the probe to succeed.  Call this
    function AFTER _login() and pass the resulting token here.

    Args:
        server: Base URL of the CIDX server.
        token:  Bearer token obtained from _login().  Without a token the
                probe will receive 401 and always return False.
        timeout: Maximum seconds to wait before giving up (default 30).
    """
    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{server}/health", headers=headers)
                if resp.status_code == 200:
                    return True
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            await asyncio.sleep(1.0)
    return False


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


async def _login(server: str, username: str, password: str) -> str:
    """Log in and return a bearer token."""
    async with httpx.AsyncClient(timeout=_LOGIN_TIMEOUT) as client:
        resp = await client.post(
            f"{server}/auth/login",
            json={"username": username, "password": password},
        )
    if resp.status_code != 200:
        log.error("Login failed: HTTP %d -- %s", resp.status_code, resp.text[:200])
        sys.exit(1)
    body = resp.json()
    token: str = body.get("access_token", "")
    if not token:
        log.error("Login response missing access_token: %s", resp.text[:200])
        sys.exit(1)
    return token


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------


async def _get_provider_embed_calls(server: str, token: str) -> Optional[int]:
    """Return provider_embed_calls from /api/admin/coalescer-metrics, or None."""
    try:
        async with httpx.AsyncClient(timeout=_METRICS_TIMEOUT) as client:
            resp = await client.get(
                f"{server}/api/admin/coalescer-metrics",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 200:
            return int(resp.json().get("provider_embed_calls", 0))
    except Exception as exc:  # noqa: BLE001
        log.debug("coalescer-metrics unavailable: %s", exc)
    return None


async def _get_cache_hit_ratio(server: str, token: str) -> Optional[float]:
    """
    Return hit_ratio from /cache/stats (HNSW index cache), or None.

    Note: This is the HNSW index cache, not the query-embedding cache.
    The query-embedding cache has no dedicated JSON REST endpoint in the current
    server; its effectiveness is inferred from provider_embed_calls instead.
    """
    try:
        async with httpx.AsyncClient(timeout=_METRICS_TIMEOUT) as client:
            resp = await client.get(
                f"{server}/cache/stats",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            ratio = data.get("hit_ratio")
            if ratio is not None:
                return float(ratio)
    except Exception as exc:  # noqa: BLE001
        log.debug("cache/stats unavailable: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Load fixture queries
# ---------------------------------------------------------------------------


def _load_queries(n: int) -> List[str]:
    """
    Load exactly n queries from the fixture file, cycling if needed.

    Falls back to built-in queries if the fixture is missing or empty.
    Raises ValueError if n < 1.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")

    lines: List[str] = []
    if _FIXTURES_PATH.exists():
        lines = [
            ln.strip() for ln in _FIXTURES_PATH.read_text().splitlines() if ln.strip()
        ]

    if not lines:
        log.warning(
            "Fixture file missing or empty (%s) -- using built-in fallback queries.",
            _FIXTURES_PATH,
        )
        lines = _FALLBACK_QUERIES

    # Cycle through available lines to fill the requested count.
    # Both branches guarantee len(lines) >= 1 so this loop terminates.
    result: List[str] = []
    idx = 0
    while len(result) < n:
        result.append(lines[idx % len(lines)])
        idx += 1
    return result


# ---------------------------------------------------------------------------
# Core scenario runner
# ---------------------------------------------------------------------------

ScenarioResult = Dict[str, Any]


async def _run_scenario(
    server: str,
    token: str,
    queries: List[str],
    concurrency: int,
    no_embedding_cache_shortcut: bool,
    scenario_name: str,
) -> ScenarioResult:
    """
    Run one benchmark scenario and return timing statistics.

    Args:
        server: Base URL of the CIDX server
        token: Bearer auth token
        queries: List of query strings to send (in order)
        concurrency: Number of simultaneous in-flight requests (>= 1)
        no_embedding_cache_shortcut: When True bypasses cache read on server
        scenario_name: Human-readable label for logging

    Returns:
        Dict with: scenario, n_requests, n_errors, throughput_rps,
                   p50_ms, p95_ms, p99_ms, elapsed_s
    """
    log.info(
        "  Scenario %-35s  queries=%d  concurrency=%d  cache_bypass=%s",
        scenario_name,
        len(queries),
        concurrency,
        no_embedding_cache_shortcut,
    )

    latencies_ms: List[float] = []
    errors = 0
    sem = asyncio.Semaphore(concurrency)

    # L2 fix: one shared client per scenario avoids 800 connection-pool setups
    # which would otherwise inflate measured latency and depress throughput.
    async with httpx.AsyncClient(timeout=_QUERY_TIMEOUT) as client:

        async def _one_request(q: str) -> None:
            nonlocal errors
            payload: Dict[str, Any] = {
                "query_text": q,
                "limit": 5,
                "no_embedding_cache_shortcut": no_embedding_cache_shortcut,
            }
            async with sem:
                t0 = time.monotonic()
                try:
                    resp = await client.post(
                        f"{server}/api/query",
                        json=payload,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if resp.status_code not in (200, 202):
                        log.debug(
                            "Query error HTTP %d: %s",
                            resp.status_code,
                            resp.text[:100],
                        )
                        errors += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("Request exception: %s", exc)
                    errors += 1
                finally:
                    elapsed = (time.monotonic() - t0) * 1000.0
                    latencies_ms.append(elapsed)

        wall_start = time.monotonic()
        tasks = [asyncio.create_task(_one_request(q)) for q in queries]
        await asyncio.gather(*tasks)

    wall_elapsed = time.monotonic() - wall_start

    n = len(queries)
    throughput = n / wall_elapsed if wall_elapsed > 0 else 0.0
    sorted_lat = sorted(latencies_ms)

    def _pct(pct: float) -> float:
        """Nearest-rank percentile (L1 fix: math.ceil avoids truncation bias)."""
        if not sorted_lat:
            return 0.0
        # nearest-rank: ceil(pct/100 * n) - 1, clamped to valid index range
        idx = max(0, math.ceil(pct / 100.0 * len(sorted_lat)) - 1)
        idx = min(idx, len(sorted_lat) - 1)
        return sorted_lat[idx]

    p50 = _pct(50)
    p95 = _pct(95)
    p99 = _pct(99)

    log.info(
        "    -> %.1f req/s  p50=%.0fms  p95=%.0fms  p99=%.0fms  errors=%d",
        throughput,
        p50,
        p95,
        p99,
        errors,
    )

    return {
        "scenario": scenario_name,
        "n_requests": n,
        "n_errors": errors,
        "throughput_rps": round(throughput, 2),
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
        "p99_ms": round(p99, 1),
        "elapsed_s": round(wall_elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Per-worker benchmark
# ---------------------------------------------------------------------------


async def _benchmark_worker_count(
    server: str,
    worker_count: int,
    n_queries: int,
    concurrency: int,
    token: str,
    unique_queries: List[str],
) -> List[ScenarioResult]:
    """Run all 4 scenarios for a given worker_count label against `server`."""
    log.info("=== Worker count: %d  server: %s ===", worker_count, server)

    repeating = [_REPEATING_QUERY] * n_queries
    unique = unique_queries[:n_queries]

    scenarios: List[Tuple[str, List[str], bool]] = [
        ("repeating+cache-on", repeating, False),
        ("repeating+cache-off", repeating, True),
        ("unique+cache-on", unique, False),
        ("unique+cache-off", unique, True),
    ]

    results: List[ScenarioResult] = []

    for scenario_name, queries, bypass_cache in scenarios:
        pre_calls = await _get_provider_embed_calls(server, token)

        result = await _run_scenario(
            server=server,
            token=token,
            queries=queries,
            concurrency=concurrency,
            no_embedding_cache_shortcut=bypass_cache,
            scenario_name=scenario_name,
        )

        post_calls = await _get_provider_embed_calls(server, token)
        hnsw_hit_ratio = await _get_cache_hit_ratio(server, token)

        provider_calls_delta: Optional[int] = None
        if pre_calls is not None and post_calls is not None:
            provider_calls_delta = post_calls - pre_calls

        # M2 fix: do NOT derive a "Cache Hit%" from provider_calls_delta.
        # provider_embed_calls increments once per sealed BATCH (not per request)
        # so under concurrency the delta is far smaller than real request misses,
        # making a derived hit-ratio wildly misleading (reports high hit% even
        # when embedding cache is OFF).  Report the raw batch-call count instead.
        result["worker_count"] = worker_count
        result["provider_embed_calls_delta"] = provider_calls_delta
        result["hnsw_hit_ratio"] = (
            round(hnsw_hit_ratio * 100.0, 1) if hnsw_hit_ratio is not None else None
        )
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _format_opt(value: Optional[Any], fmt: str = "{}") -> str:
    if value is None:
        return "n/a"
    return fmt.format(value)


def _build_markdown_report(
    all_results: List[ScenarioResult],
    run_meta: Dict[str, Any],
) -> str:
    """Build a markdown report from all benchmark results."""
    lines: List[str] = []
    lines.append("# Multi-Worker Throughput Benchmark Report")
    lines.append("")
    lines.append(f"Generated: {run_meta['timestamp']}")
    lines.append(f"Server: {run_meta['server']}")
    lines.append(f"Queries per scenario: {run_meta['queries_per_scenario']}")
    lines.append(f"Concurrency: {run_meta['concurrency']}")
    lines.append(f"Regression multiplier: {run_meta['regression_multiplier']}x")
    lines.append("")
    lines.append(
        "NOTE: 'Provider Embed Calls (batches)' is the raw delta from "
        "/api/admin/coalescer-metrics between scenario start and end. "
        "This counter increments once per sealed BATCH, not per request, "
        "so under concurrency coalescing makes it much smaller than the number "
        "of real request misses. It is a per-node, dedup-influenced count and "
        "should NOT be interpreted as a cache-miss ratio. "
        "'HNSW Hit%' is the genuine HNSW index cache hit ratio from /cache/stats."
    )
    lines.append("")

    lines.append(
        "| Workers | Scenario | Cache | Req/s | p50 (ms) | p95 (ms) | p99 (ms) "
        "| Provider Embed Calls (batches) | HNSW Hit% |"
    )
    lines.append(
        "|--------:|----------|-------|------:|---------:|---------:|---------:"
        "|-------------------------------:|----------:|"
    )

    for r in all_results:
        scenario = r["scenario"]
        parts = scenario.split("+")
        query_type = parts[0] if parts else scenario
        cache_label = parts[1] if len(parts) > 1 else ""

        lines.append(
            f"| {r['worker_count']} "
            f"| {query_type} "
            f"| {cache_label} "
            f"| {r['throughput_rps']:.1f} "
            f"| {r['p50_ms']:.0f} "
            f"| {r['p95_ms']:.0f} "
            f"| {r['p99_ms']:.0f} "
            f"| {_format_opt(r.get('provider_embed_calls_delta'))} "
            f"| {_format_opt(r.get('hnsw_hit_ratio'), '{:.1f}%')} "
            "|"
        )

    lines.append("")

    if "regression_result" in run_meta:
        reg = run_meta["regression_result"]
        lines.append("## Regression Check")
        lines.append("")
        if reg.get("ran"):
            status = "PASSED" if reg["passed"] else "FAILED"
            lines.append(f"Status: **{status}**")
            lines.append(f"2-worker throughput: {reg['two_worker_rps']:.1f} req/s")
            lines.append(f"1-worker throughput: {reg['one_worker_rps']:.1f} req/s")
            lines.append(
                f"Actual ratio: {reg['actual_ratio']:.2f}x  "
                f"(required >= {reg['required_ratio']}x)"
            )
        else:
            lines.append(
                "Regression check skipped (need both worker counts 1 and 2 in matrix)."
            )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Regression check
# ---------------------------------------------------------------------------


def _run_regression_check(
    all_results: List[ScenarioResult],
    multiplier: float,
) -> Dict[str, Any]:
    """
    Assert 2-worker repeating+cache-on throughput >= multiplier * 1-worker.

    Returns a dict describing the outcome.
    """
    target_scenario = "repeating+cache-on"
    one_worker_rps: Optional[float] = None
    two_worker_rps: Optional[float] = None

    for r in all_results:
        if r["scenario"] == target_scenario:
            if r["worker_count"] == 1:
                one_worker_rps = r["throughput_rps"]
            elif r["worker_count"] == 2:
                two_worker_rps = r["throughput_rps"]

    if one_worker_rps is None or two_worker_rps is None:
        return {"ran": False}

    actual_ratio = two_worker_rps / one_worker_rps if one_worker_rps > 0 else 0.0
    passed = actual_ratio >= multiplier

    return {
        "ran": True,
        "passed": passed,
        "one_worker_rps": one_worker_rps,
        "two_worker_rps": two_worker_rps,
        "actual_ratio": actual_ratio,
        "required_ratio": multiplier,
    }


# ---------------------------------------------------------------------------
# Report persistence
# ---------------------------------------------------------------------------


def _save_reports(
    all_results: List[ScenarioResult],
    run_meta: Dict[str, Any],
) -> Tuple[Path, Path]:
    """Save markdown and JSON reports; return (md_path, json_path)."""
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = run_meta["timestamp"].replace(":", "-").replace(" ", "_")[:19]
    base = f"multi_worker_throughput_{ts}"
    md_path = _REPORTS_DIR / f"{base}.md"
    json_path = _REPORTS_DIR / f"{base}.json"

    md_path.write_text(_build_markdown_report(all_results, run_meta))
    json_path.write_text(
        json.dumps({"meta": run_meta, "results": all_results}, indent=2, default=str)
    )

    log.info("Report saved: %s", md_path)
    log.info("Report saved: %s", json_path)
    return md_path, json_path


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def _parse_workers(raw: str) -> List[int]:
    """Parse '1,2,3,4' or '2' into a list of positive ints."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("At least one worker count is required.")
    result: List[int] = []
    for p in parts:
        try:
            n = int(p)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"Invalid worker count: {p!r}. Must be a positive integer."
            )
        if n < 1:
            raise argparse.ArgumentTypeError(f"Worker count must be >= 1, got {n}.")
        result.append(n)
    return result


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="multi_worker_throughput.py",
        description=(
            "Multi-worker search throughput benchmark for CIDX server (Story #1168). "
            "Measures req/s across 4 scenarios per worker count. "
            "Does NOT start or stop servers -- operator must manage server lifecycle."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # Single worker count against an already-running server on :8001
  python3 scripts/analysis/multi_worker_throughput.py \\
      --server http://localhost:8001 --workers 2 --queries 200 --concurrency 20

  # Full 1/2/3/4 matrix (server handles all runs consecutively)
  python3 scripts/analysis/multi_worker_throughput.py \\
      --server http://localhost:8000 --workers 1,2,3,4

  # Quick smoke test with small query count
  python3 scripts/analysis/multi_worker_throughput.py \\
      --server http://localhost:8000 --workers 1 --queries 10 --concurrency 4

Server Management
-----------------
For each entry in --workers the script uses the SAME --server URL.
To test different worker counts:
  1. Stop the server.
  2. Restart with desired --workers N.
  3. Re-invoke this script for each count.

NEVER target or restart the developer server on :8000. Use an isolated port.
""",
    )
    p.add_argument(
        "--server",
        required=True,
        help=(
            "Base URL of the CIDX server to benchmark (REQUIRED). "
            "Must be an isolated benchmark server -- NEVER point this at the "
            "developer server on :8000. Example: http://localhost:8105"
        ),
    )
    p.add_argument(
        "--workers",
        default="1",
        type=str,
        help=(
            "Comma-separated worker counts, e.g. '1,2,3,4' or '2'. "
            "METADATA only -- script does not restart servers. (default: 1)"
        ),
    )
    p.add_argument(
        "--queries",
        type=int,
        default=200,
        help="Number of queries per scenario, >= 1 (default: 200)",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=20,
        help="Number of simultaneous in-flight requests, >= 1 (default: 20)",
    )
    p.add_argument(
        "--regression-multiplier",
        type=float,
        default=1.7,
        dest="regression_multiplier",
        help=(
            "Required 2-worker/1-worker throughput ratio for regression check. "
            "Must be > 0. (default: 1.7)"
        ),
    )
    p.add_argument(
        "--local-testing",
        default=".local-testing",
        dest="local_testing",
        help="Path to local-testing credentials file (default: .local-testing)",
    )
    p.add_argument(
        "--no-wait-health",
        action="store_true",
        dest="no_wait_health",
        help="Skip the /health readiness probe before running benchmarks",
    )
    return p


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def _async_main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Validate numeric args
    try:
        worker_counts = _parse_workers(args.workers)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
        return 2  # unreachable but satisfies type checker

    if args.queries < 1:
        parser.error("--queries must be >= 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.regression_multiplier <= 0:
        parser.error("--regression-multiplier must be > 0")

    server: str = args.server.rstrip("/")
    n_queries: int = args.queries
    concurrency: int = args.concurrency
    multiplier: float = args.regression_multiplier

    log.info("=== CIDX Multi-Worker Throughput Benchmark ===")
    log.info("Server:      %s", server)
    log.info(
        "Workers:     %s (metadata only -- operator manages server)", worker_counts
    )
    log.info("Queries:     %d per scenario", n_queries)
    log.info("Concurrency: %d", concurrency)
    log.info("Multiplier:  %.1fx", multiplier)

    username, password = _load_credentials(args.local_testing)

    # Login FIRST so we have a token for the authenticated health probe.
    # /health requires Depends(get_current_user), so an unauthenticated
    # probe always returns 401 and the wait loop would time out and abort.
    log.info("Authenticating as %s ...", username)
    token = await _login(server, username, password)
    log.info("Authenticated.")

    if not args.no_wait_health:
        log.info("Probing server health at %s/health ...", server)
        ready = await _wait_for_health(server, token=token, timeout=30.0)
        if not ready:
            log.error("Server at %s is not healthy after 30s -- aborting.", server)
            return 1
        log.info("Server is healthy.")

    unique_queries = _load_queries(n_queries)

    all_results: List[ScenarioResult] = []
    for wc in worker_counts:
        results = await _benchmark_worker_count(
            server=server,
            worker_count=wc,
            n_queries=n_queries,
            concurrency=concurrency,
            token=token,
            unique_queries=unique_queries,
        )
        all_results.extend(results)

    reg_result = _run_regression_check(all_results, multiplier)

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    run_meta: Dict[str, Any] = {
        "timestamp": timestamp,
        "server": server,
        "worker_counts": worker_counts,
        "queries_per_scenario": n_queries,
        "concurrency": concurrency,
        "regression_multiplier": multiplier,
        "regression_result": reg_result,
    }

    md_path, _json_path = _save_reports(all_results, run_meta)

    print("")
    print(_build_markdown_report(all_results, run_meta))
    print(f"Reports written to: {md_path.parent}/")

    if reg_result.get("ran"):
        if reg_result["passed"]:
            log.info(
                "REGRESSION CHECK PASSED: %.2fx >= %.1fx",
                reg_result["actual_ratio"],
                multiplier,
            )
            return 0
        else:
            log.error(
                "REGRESSION CHECK FAILED: %.2fx < %.1fx required",
                reg_result["actual_ratio"],
                multiplier,
            )
            return 1

    log.info("Regression check skipped (need both worker counts 1 and 2 in matrix).")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point callable from tests or CLI."""
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
