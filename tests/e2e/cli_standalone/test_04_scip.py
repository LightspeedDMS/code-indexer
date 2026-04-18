"""
Phase 1 — AC1-AC4: SCIP Intelligence tests.

Tests cover the full SCIP lifecycle:
  AC1 — Generation: scip generate on Python (markupsafe) and TypeScript (type-fest) repos
  AC2 — Queries: definition, references, dependencies, dependents, impact, callchain, context
  AC3 — Maintenance: rebuild --failed, verify
  AC4 — Graceful non-SCIP handling: tries (Pascal/Delphi) produces no Python traceback

All tests run real CLI subprocesses against real working copies.  No mocking.
VOYAGE_API_KEY is not required for SCIP tests (SCIP uses symbolic analysis, not embeddings).

Real behavior notes (discovered during probe runs):
  - scip generate: rc=0 even on failure; status command reports SUCCESS/FAILED/PENDING
  - type-fest (TypeScript without npm install): rc=0, status=FAILED — graceful toolchain error
  - tries (Pascal): rc=0, "No buildable projects discovered" — unsupported language
  - scip verify: requires original .scip protobuf; DELETED after conversion -> always rc=1
                 post-generate; test asserts graceful error (no Python traceback)
  - scip rebuild --failed when nothing failed: rc=0, no-op
  - scip dependencies on a leaf class: rc=0, "No dependencies found" is valid
  - scip impact on a top-level symbol: rc=0, "No dependents found" is valid

Flag audit deviations from story spec:
  - spec referenced test_03_scip.py but test_03_utilities.py already exists -> renamed test_04_scip.py
  - scip verify requires DATABASE_PATH positional arg (spec implied no-arg form)
  - scip verify always fails post-generate (source .scip deleted); test accepts rc!=0

Total: 13 test cases.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e.helpers import run_cidx


# ---------------------------------------------------------------------------
# Timeout constants
# ---------------------------------------------------------------------------

SCIP_GENERATE_TIMEOUT: int = 300
"""Maximum seconds for scip generate (Python SCIP indexer can take ~15s)."""

SCIP_GENERATE_TRIES_TIMEOUT: int = 120
"""Maximum seconds for scip generate on tries (unsupported language, near instant)."""


# ---------------------------------------------------------------------------
# Local helpers
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


def has_python_traceback(text: str) -> bool:
    """Return True if text contains a Python traceback signature."""
    return "Traceback (most recent call last)" in text


def assert_scip_db_exists(repo_path: Path) -> list[Path]:
    """Assert at least one .scip.db file exists under repo_path/.code-indexer/.

    Returns the list of found .scip.db paths so callers can inspect them if
    needed.  Raises AssertionError with a descriptive message on failure.
    """
    scip_db_files = list(repo_path.glob(".code-indexer/**/*.scip.db"))
    assert scip_db_files, (
        f"No .scip.db files found under {repo_path}/.code-indexer/\n"
        f"Contents: {list((repo_path / '.code-indexer').iterdir())}"
    )
    return scip_db_files


def run_cidx_with_timeout(
    *args: str,
    cwd: Path,
    env: dict[str, str],
    timeout: int = SCIP_GENERATE_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run cidx CLI with an explicit subprocess timeout.

    run_cidx() from helpers does not expose the subprocess timeout parameter.
    For long-running operations like scip generate we call subprocess.run
    directly with the same command construction pattern.
    """
    cmd = [sys.executable, "-m", "code_indexer.cli"] + list(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=timeout,
    )


def _ensure_initialized(path: Path, env: dict[str, str]) -> None:
    """Idempotently run ``cidx init`` if ``.code-indexer/`` does not exist.

    Shared by fixtures and tests that need a working copy initialized before
    running SCIP commands.  Raises AssertionError on init failure.
    """
    if (path / ".code-indexer").is_dir():
        return
    result = run_cidx("init", cwd=path, env=env)
    assert result.returncode == 0, (
        f"cidx init failed for {path}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Session fixtures — SCIP generation (once per session per repo)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def scip_markupsafe(
    indexed_markupsafe: Path,
    e2e_cli_env: dict[str, str],
) -> Path:
    """Generate SCIP index on markupsafe once per session.

    Idempotent: if the .scip.db file already exists from a prior run the
    generate command still succeeds (overwrites) within the timeout.
    Returns the markupsafe working-copy path for use by AC1 and AC2 tests.
    """
    result = run_cidx_with_timeout(
        "scip", "generate",
        cwd=indexed_markupsafe,
        env=e2e_cli_env,
        timeout=SCIP_GENERATE_TIMEOUT,
    )
    assert result.returncode == 0, (
        f"scip generate on markupsafe failed (rc={result.returncode})\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return indexed_markupsafe


@pytest.fixture(scope="session")
def scip_typefest(
    e2e_seed_repo_paths,
    e2e_cli_env: dict[str, str],
) -> Path:
    """Initialize and attempt SCIP generation on type-fest once per session.

    type-fest is a TypeScript project.  Without ``npm install`` the
    scip-typescript indexer fails gracefully (no Python traceback).  The CLI
    may exit with rc=0 or rc=1 depending on whether projects were discovered;
    both are acceptable.  The fixture only asserts no Python traceback.
    """
    path = e2e_seed_repo_paths.type_fest
    _ensure_initialized(path, e2e_cli_env)

    result = run_cidx_with_timeout(
        "scip", "generate",
        cwd=path,
        env=e2e_cli_env,
        timeout=SCIP_GENERATE_TIMEOUT,
    )
    combined = result.stdout + result.stderr
    assert not has_python_traceback(combined), (
        f"scip generate on type-fest produced a Python traceback\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return path


# ---------------------------------------------------------------------------
# AC1 Tests — SCIP Generation
# ---------------------------------------------------------------------------


def test_scip_generate_markupsafe(
    scip_markupsafe: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """scip generate on markupsafe exits 0 and status reports SUCCESS.

    Validates the generation outcome via ``scip status``.  The filesystem
    artifact (.scip.db presence) is covered separately by test_scip_db_files_created.
    """
    result = run_cidx("scip", "status", cwd=scip_markupsafe, env=e2e_cli_env)
    assert_cidx_ok(result, context="scip status markupsafe")
    combined = result.stdout + result.stderr
    assert "SUCCESS" in combined, (
        f"Expected 'SUCCESS' in scip status after generate on markupsafe.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_scip_generate_type_fest(
    scip_typefest: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """scip generate on type-fest exits 0 but status is FAILED (no npm install).

    Without ``npm install`` the scip-typescript indexer cannot run.  The CLI
    exits 0 with a graceful FAILED status — no Python traceback.
    """
    result = run_cidx("scip", "status", cwd=scip_typefest, env=e2e_cli_env)
    assert_cidx_ok(result, context="scip status type-fest")
    combined = result.stdout + result.stderr
    assert "FAILED" in combined, (
        f"Expected 'FAILED' in scip status for type-fest (npm install missing).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert not has_python_traceback(combined), (
        f"scip generate on type-fest produced a Python traceback.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_scip_db_files_created(scip_markupsafe: Path) -> None:
    """After scip generate, at least one .scip.db file exists under .code-indexer/.

    Sole filesystem artifact check for the markupsafe SCIP generation result.
    """
    assert_scip_db_exists(scip_markupsafe)


# ---------------------------------------------------------------------------
# AC2 Tests — SCIP Queries (all depend on scip_markupsafe fixture)
# ---------------------------------------------------------------------------


def test_scip_definition_markup(
    scip_markupsafe: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """scip definition Markup exits 0 and returns non-empty output.

    'Markup' is a real class defined in markupsafe/__init__.py.
    """
    result = run_cidx(
        "scip", "definition", "Markup",
        cwd=scip_markupsafe,
        env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="scip definition Markup")
    assert result.stdout.strip(), (
        "scip definition Markup returned empty output — expected symbol locations"
    )


def test_scip_references_markup(
    scip_markupsafe: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """scip references Markup exits 0 and returns non-empty output.

    'Markup' is widely referenced throughout the markupsafe codebase.
    """
    result = run_cidx(
        "scip", "references", "Markup",
        cwd=scip_markupsafe,
        env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="scip references Markup")
    assert result.stdout.strip(), (
        "scip references Markup returned empty output — expected reference locations"
    )


def test_scip_dependencies_markup(
    scip_markupsafe: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """scip dependencies Markup exits 0.

    Markup is a leaf class with no outgoing dependencies in the call graph so
    empty output is valid.  We only assert rc=0 (no crash, no traceback).
    """
    result = run_cidx(
        "scip", "dependencies", "Markup",
        cwd=scip_markupsafe,
        env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="scip dependencies Markup")


def test_scip_dependents_markup(
    scip_markupsafe: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """scip dependents Markup exits 0."""
    result = run_cidx(
        "scip", "dependents", "Markup",
        cwd=scip_markupsafe,
        env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="scip dependents Markup")


def test_scip_impact_markup(
    scip_markupsafe: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """scip impact Markup exits 0.

    Impact analysis may report 'No dependents found' for top-level symbols;
    that is valid CLI behaviour.  We assert rc=0 only.
    """
    result = run_cidx(
        "scip", "impact", "Markup",
        cwd=scip_markupsafe,
        env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="scip impact Markup")


def test_scip_callchain_escape_markup(
    scip_markupsafe: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """scip callchain escape Markup exits 0 and returns non-empty output.

    There are call paths from escape() to Markup in markupsafe (verified
    during probe run: 228 chains found).  We assert rc=0 and non-empty output.
    """
    result = run_cidx(
        "scip", "callchain", "escape", "Markup",
        cwd=scip_markupsafe,
        env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="scip callchain escape Markup")
    assert result.stdout.strip(), (
        "scip callchain escape Markup returned empty output — expected chain results"
    )


def test_scip_context_markup(
    scip_markupsafe: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """scip context Markup exits 0 and returns non-empty output.

    Context combines definition + references + dependencies into a curated
    file list — always non-empty for a class that exists in the codebase.
    """
    result = run_cidx(
        "scip", "context", "Markup",
        cwd=scip_markupsafe,
        env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="scip context Markup")
    assert result.stdout.strip(), (
        "scip context Markup returned empty output — expected relevant file list"
    )


# ---------------------------------------------------------------------------
# AC3 Tests — SCIP Maintenance
# ---------------------------------------------------------------------------


def test_scip_rebuild_failed_noop(
    scip_markupsafe: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """scip rebuild --failed exits 0 when no projects have failed.

    markupsafe SCIP generation succeeds so --failed is a no-op.
    CLI outputs 'No failed projects to rebuild'.
    """
    result = run_cidx(
        "scip", "rebuild", "--failed",
        cwd=scip_markupsafe,
        env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="scip rebuild --failed")


def test_scip_verify_graceful(
    scip_markupsafe: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """scip verify against the .scip.db produces a graceful error (no traceback).

    scip verify requires the original .scip protobuf file which is DELETED
    after database conversion (per project CLAUDE.md: 'SCIP files are DELETED
    after database conversion').  So verify always fails post-generate with a
    clean 'Corresponding SCIP file not found' error message.

    We assert: (a) command does not hang, (b) no Python traceback in output.
    We do NOT assert rc=0 since rc=1 is the documented behaviour when the
    source .scip file has been deleted.
    """
    db_files = assert_scip_db_exists(scip_markupsafe)
    db_path = db_files[0]

    result = run_cidx(
        "scip", "verify", str(db_path),
        cwd=scip_markupsafe,
        env=e2e_cli_env,
    )
    # rc=1 expected (source .scip deleted after conversion)
    combined = result.stdout + result.stderr
    assert not has_python_traceback(combined), (
        f"scip verify produced a Python traceback (unexpected crash)\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# AC4 Test — Graceful Non-SCIP Handling
# ---------------------------------------------------------------------------


def test_scip_generate_tries_pascal_graceful(
    e2e_seed_repo_paths,
    e2e_cli_env: dict[str, str],
) -> None:
    """scip generate on tries (Pascal/Delphi) exits gracefully with 'No buildable projects discovered'.

    Pascal is not a supported SCIP language.  The CLI reports 'No buildable
    projects discovered' and exits without a Python traceback.  The exit code
    may be 0 or 1 depending on how the status file records an empty run; both
    are acceptable — what matters is the graceful message and absence of crash.

    Assertions:
      - command completes within timeout (no hang)
      - output contains 'No buildable projects discovered'
      - no Python traceback in stdout or stderr
    """
    path = e2e_seed_repo_paths.tries
    _ensure_initialized(path, e2e_cli_env)

    result = run_cidx_with_timeout(
        "scip", "generate",
        cwd=path,
        env=e2e_cli_env,
        timeout=SCIP_GENERATE_TRIES_TIMEOUT,
    )
    combined = result.stdout + result.stderr
    assert "No buildable projects discovered" in combined, (
        f"Expected 'No buildable projects discovered' for Pascal repo.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert not has_python_traceback(combined), (
        f"scip generate on tries produced a Python traceback.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
