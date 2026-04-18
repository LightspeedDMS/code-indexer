"""
Phase 1 — AC2: Query tests.

All tests depend on the ``indexed_markupsafe`` session fixture from
``conftest.py`` which runs ``cidx init`` + ``cidx index`` on the markupsafe
working copy exactly once per session.  Every test is a pure query — no
mutations to the index.

Real VoyageAI embeddings are used (VOYAGE_API_KEY must be set).  No mocking.

Result format (--quiet mode):
    N. 0.NNN <status> path:line
    <content lines>

Each result starts with ``N. `` (result number, dot, space).  We use this
to count results reliably in ``test_query_limit``.

Total: 9 test cases.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.e2e.helpers import run_cidx


# ---------------------------------------------------------------------------
# Local helper
# ---------------------------------------------------------------------------


def assert_cidx_ok(
    result: subprocess.CompletedProcess[str], *, context: str = ""
) -> None:
    """Assert that a cidx subprocess completed with exit code 0."""
    prefix = f"{context}: " if context else ""
    assert result.returncode == 0, (
        f"{prefix}cidx exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def _count_results(output: str) -> int:
    """Count result entries in --quiet output.

    Each result starts with a line of the form ``N. 0.NNN ...`` where N is
    the 1-based result number.  We count lines matching ``^\\d+\\.\\s`` as
    result headers.
    """
    return len(re.findall(r"^\d+\.\s", output, re.MULTILINE))


# ---------------------------------------------------------------------------
# AC2: Query tests — all depend on indexed_markupsafe fixture
# ---------------------------------------------------------------------------


def test_query_semantic(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """Semantic query 'escape HTML' exits 0 and returns non-empty output."""
    result = run_cidx(
        "query", "escape HTML", "--quiet",
        cwd=indexed_markupsafe, env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="query semantic")
    assert result.stdout.strip(), (
        "Semantic query returned empty output — expected at least one result"
    )


def test_query_fts(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """FTS query for 'Markup' exits 0 and returns non-empty output."""
    result = run_cidx(
        "query", "Markup", "--fts", "--quiet",
        cwd=indexed_markupsafe, env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="query fts")
    assert result.stdout.strip(), (
        "FTS query returned empty output — expected at least one result"
    )


def test_query_fts_regex(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """FTS regex query 'def.*escape' exits 0 (empty results acceptable since regex may not match)."""
    result = run_cidx(
        "query", "def.*escape", "--fts", "--regex", "--quiet",
        cwd=indexed_markupsafe, env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="query fts regex")


def test_query_time_range_all(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """Query with --time-range-all exits 0 (queries entire temporal history)."""
    result = run_cidx(
        "query", "escape", "--time-range-all", "--quiet",
        cwd=indexed_markupsafe, env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="query time-range-all")


def test_query_language_python(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """Query filtered to Python files exits 0 and returns non-empty output."""
    result = run_cidx(
        "query", "escape", "--language", "python", "--quiet",
        cwd=indexed_markupsafe, env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="query language python")
    assert result.stdout.strip(), (
        "Language-filtered query returned empty output — markupsafe has Python files"
    )


def test_query_path_filter(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """Query with --path-filter '*/tests/*' exits 0."""
    result = run_cidx(
        "query", "escape", "--path-filter", "*/tests/*", "--quiet",
        cwd=indexed_markupsafe, env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="query path-filter")


def test_query_min_score(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """Query with --min-score 0.5 exits 0."""
    result = run_cidx(
        "query", "escape", "--min-score", "0.5", "--quiet",
        cwd=indexed_markupsafe, env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="query min-score")


def test_query_limit(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """Query with --limit 3 exits 0 and returns at most 3 results.

    Result entries in --quiet mode start with ``N. 0.NNN ...`` lines.
    We count those headers to determine the actual result count and assert
    it does not exceed the requested limit.  The test fails explicitly when
    output is non-empty but no result headers are found, catching format
    changes early.
    """
    result = run_cidx(
        "query", "escape", "--limit", "3", "--quiet",
        cwd=indexed_markupsafe, env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="query limit")

    output = result.stdout
    if output.strip():
        count = _count_results(output)
        assert count > 0, (
            "Output is non-empty but no result headers ('^N. ') found — "
            "output format may have changed.\n"
            f"output: {output[:300]}"
        )
        assert count <= 3, (
            f"Expected at most 3 results with --limit 3, got {count}\n"
            f"output: {output[:300]}"
        )


def test_query_accuracy_high(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """Query with --accuracy high exits 0."""
    result = run_cidx(
        "query", "escape", "--accuracy", "high", "--quiet",
        cwd=indexed_markupsafe, env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="query accuracy high")
