"""
Multi-worker scaling benchmark pytest wrapper (Story #1168).

This test is a gate-level integration test that invokes the standalone benchmark
script ``scripts/analysis/multi_worker_throughput.py`` as a subprocess and
asserts that it exits 0 (i.e. the regression check passed).

Skip conditions
---------------
- ``CIDX_PERF_TEST=1`` is NOT set in the environment (default behaviour so that
  this test never runs in fast-automation.sh or server-fast-automation.sh).
- The benchmark script file does not exist on disk.

Anti-mock guarantee
-------------------
No mocking of the CIDX server or the VoyageAI/Cohere embedding provider.
The test requires a live server at the URL carried by ``CIDX_BENCH_SERVER``
(default ``http://localhost:8000``).

Environment variables
---------------------
CIDX_PERF_TEST      Set to "1" to enable this test suite.
CIDX_BENCH_SERVER   Server URL (REQUIRED -- must be an isolated benchmark
                    server, NEVER the developer :8000 instance).
                    Example: http://localhost:8105
CIDX_BENCH_WORKERS  Comma-separated worker counts (default: "1,2").
CIDX_BENCH_QUERIES  Queries per scenario (default: "50").
CIDX_BENCH_CONCURRENCY  Concurrent requests (default: "10").

Operator note
-------------
The FULL 1/2/3/4-worker benchmark with the 1.7x assertion is the operator
gate; this test may run a smaller subset (e.g. --workers 1,2 --queries 50)
to keep CI time bounded.  The operator gate is documented in docs/perf-benchmark.md.

CIDX_BENCH_SERVER has no safe default: the script's --server is now a required
argument.  Set CIDX_BENCH_SERVER to a dedicated isolated server before running.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_BENCHMARK_SCRIPT = (
    Path(__file__).parent.parent.parent
    / "scripts"
    / "analysis"
    / "multi_worker_throughput.py"
)

# ---------------------------------------------------------------------------
# Skip gate
# ---------------------------------------------------------------------------

_PERF_TEST_ENABLED = os.environ.get("CIDX_PERF_TEST", "") == "1"

_SKIP_REASON = (
    "Performance tests are disabled. Set CIDX_PERF_TEST=1 to enable. "
    "Requires a live CIDX server (default: http://localhost:8000). "
    "This test intentionally does NOT run in fast-automation.sh or "
    "server-fast-automation.sh."
)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.performance
@pytest.mark.skipif(not _PERF_TEST_ENABLED, reason=_SKIP_REASON)
def test_benchmark_script_runs_and_passes() -> None:
    """
    Invoke the benchmark script as a subprocess and assert exit code 0.

    Uses a small query count / concurrency so the test completes quickly.
    The regression check (2-worker >= 1.7x 1-worker) is embedded in the
    script; exit 0 means it passed.

    Requires CIDX_PERF_TEST=1 and a live server.
    """
    if not _BENCHMARK_SCRIPT.exists():
        pytest.fail(
            f"Benchmark script not found: {_BENCHMARK_SCRIPT}. "
            "Ensure scripts/analysis/multi_worker_throughput.py is present."
        )

    server = os.environ.get("CIDX_BENCH_SERVER")
    if not server:
        pytest.skip(
            "CIDX_BENCH_SERVER not set; refusing to default to the protected dev server on :8000"
        )
    workers = os.environ.get("CIDX_BENCH_WORKERS", "1,2")
    queries = os.environ.get("CIDX_BENCH_QUERIES", "50")
    concurrency = os.environ.get("CIDX_BENCH_CONCURRENCY", "10")

    cmd = [
        sys.executable,
        str(_BENCHMARK_SCRIPT),
        "--server",
        server,
        "--workers",
        workers,
        "--queries",
        queries,
        "--concurrency",
        concurrency,
    ]

    result = subprocess.run(
        cmd,
        capture_output=False,  # let stdout/stderr flow to test output
        timeout=600,  # 10 min hard cap
    )

    assert result.returncode == 0, (
        f"Benchmark script exited with code {result.returncode}. "
        "Check output above for details. "
        "Exit 1 means the regression check failed (2-worker < 1.7x 1-worker) "
        "or a connection/auth error occurred."
    )


@pytest.mark.performance
@pytest.mark.skipif(not _PERF_TEST_ENABLED, reason=_SKIP_REASON)
def test_benchmark_script_help_runs() -> None:
    """
    Smoke-test that --help exits 0 without needing a live server.

    This validates that the script is importable and the CLI is wired correctly.
    Only runs when CIDX_PERF_TEST=1 because the suite is gated at that level.
    """
    if not _BENCHMARK_SCRIPT.exists():
        pytest.fail(f"Benchmark script not found: {_BENCHMARK_SCRIPT}")

    result = subprocess.run(
        [sys.executable, str(_BENCHMARK_SCRIPT), "--help"],
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"--help exited {result.returncode}. stderr: {result.stderr.decode()[:500]}"
    )
    assert (
        b"usage" in result.stdout.lower() or b"multi_worker_throughput" in result.stdout
    ), "--help output did not contain expected usage text"
