"""AC-B: real end-to-end proof of Issue #1601's bounded-read fix.

Two test classes, honestly separated by what they actually prove (Priority
5 of the Issue #1601 remediation -- naming/claims must not overstate):

- ``TestRegexSearchIntegrationBoundedReadSmallCeiling``: an
  integration/unit-style test. It patches the internal ``_MAX_READ_BYTES``
  constant down to a small value -- a DELIBERATE, DOCUMENTED choice for
  fast execution, not a claim of proof at the real production ceiling.
  RegexSearchService, SubprocessExecutor, real ``rg``, and repository-path
  resolution are all still real; only the byte ceiling is a test value.

- ``TestRegexSearchE2EBoundedReadRealCeiling``: genuinely zero-mock,
  real front-door E2E. It exercises the REAL, unpatched 64 MiB
  ``_MAX_READ_BYTES`` production constant against a real ~85+ MiB rg JSON
  output fixture, with direct byte-count evidence obtained via pure
  filesystem OBSERVATION (a concurrent task polls a real, ISOLATED temp
  directory while the real subprocess runs, recording the maximum real
  on-disk size ever seen for the search's own output file). No
  ``unittest.mock`` usage anywhere in this class.

Both classes exercise the real regex_search MCP tool end-to-end: real
`rg` subprocess, real file I/O, no mocking of RegexSearchService or its
subprocess execution, and no mocking of repository-path resolution either
-- the fixture repo is a genuine git repository, and its absolute path is
passed directly as the ``repository_alias`` (a full, non "-global" path),
which _legacy._resolve_repo_path resolves via its own real "Try as full
path first" branch (a real ``_is_git_repo()`` check), with no golden-repo
registry involved at all.

The config service is a REAL ``ConfigService`` instance backed by a real
(temporary) server directory -- installed as the process-wide singleton
via ``set_config_service``/``reset_config_service`` (the module's own
documented test seam for exactly this case), with the PREVIOUS singleton
captured and restored afterward (never unconditionally reset). Restoring
``app.state.golden_repos_dir`` distinguishes "the attribute was absent
before" from "the attribute was explicitly None" via a sentinel, so
teardown never leaks a spurious ``None`` attribute into later tests.
``ConfigService.load_config()`` never touches a database when no runtime
DB pool/SQLite path has been configured (the case here), so this stays
lightweight with no DB bootstrap required. All process-wide state
mutation (``app.state``, the ConfigService singleton, ``tempfile.tempdir``)
is guarded by one shared ``asyncio.Lock()``, acquired EXACTLY ONCE per
test (never nested -- asyncio.Lock is not reentrant, so the real-ceiling
test uses a single combined bootstrap context manager rather than
composing two separately-locking ones), held across the full awaited
critical section so concurrent asyncio tasks on the same thread cannot
race each other.

Slow tier: real subprocess + a few hundred real files. Excluded from
fast-automation.sh's default run via @pytest.mark.slow.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

import code_indexer.server.services.config_service as config_service_module
from code_indexer.server import app as app_module
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.search import handle_regex_search
from code_indexer.server.services.config_service import (
    ConfigService,
    reset_config_service,
    set_config_service,
)

# Fixture tuning: enough files/lines with a broad, non-selective pattern
# (character-class repetition, no fixed literal substring) that ripgrep's
# --json output genuinely exceeds the small test byte ceiling below --
# proven empirically in the test itself (AC-B1), not merely asserted.
_FIXTURE_FILE_COUNT = 300
_FIXTURE_LINES_PER_FILE = 40
_TEST_BYTE_CEILING = 16 * 1024  # 16 KiB -- small enough for a fast slow-tier test
_BROAD_PATTERN = r"[a-z]{4,}"  # matches almost every line of generated content
# Not a real bcrypt hash -- only used to satisfy the User dataclass's
# required field for this in-process call; never checked against anything.
_PLACEHOLDER_PASSWORD_HASH = "not-a-real-hash-test-placeholder"

# Real-production-ceiling fixture. ~200,000 matching lines of real rg
# --json output comfortably exceeds the real 64 MiB _MAX_READ_BYTES
# ceiling (benchmarked: 300,000 such lines produced ~124 MiB in well
# under a second).
_REAL_CEILING_LINE_COUNT = 200_000
_REAL_MAX_READ_BYTES = 64 * 1024 * 1024
# Generous safety margin above the real ceiling for the observed max temp
# file size: the write-time cap bounds overshoot to roughly one internal
# read-chunk (tens of KiB), comfortably under 1 MiB.
_POLL_SIZE_SLACK_BYTES = 1024 * 1024
# Issue #1601 remediation round 4 (Priority 5): lower-bound floor for the
# observed capped size, as a fraction of the real ceiling. 0.5 (half the
# real 64 MiB ceiling, i.e. at least 32 MiB) comfortably absorbs buffered-
# writer lag and poll-timing slack while still failing hard against an
# order-of-magnitude-smaller misconfigured ceiling (e.g. 16 KiB) -- the
# exact class of bug an upper-bound-only assertion cannot catch.
_MIN_OBSERVED_FRACTION_OF_REAL_CEILING = 0.5
_TEMP_FILE_POLL_INTERVAL_SECONDS = 0.002
_POLL_DEADLINE_SECONDS = 90

# Guards all process-wide state mutation this file's tests perform
# (app.state.golden_repos_dir, the ConfigService singleton,
# tempfile.tempdir), held across the full awaited critical section so
# concurrent asyncio tasks cannot race. Acquired EXACTLY ONCE per test --
# asyncio.Lock is not reentrant.
_GLOBAL_STATE_LOCK = asyncio.Lock()

# Sentinel distinguishing "app.state.golden_repos_dir was absent" from
# "it was explicitly None", so teardown restores the exact prior state
# (delattr vs. reassignment) instead of leaking a spurious None.
_MISSING = object()


def _make_user() -> User:
    from datetime import datetime, timezone

    return User(
        username="e2e_test_user",
        password_hash=_PLACEHOLDER_PASSWORD_HASH,
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _generate_fixture_repo(repo_path) -> None:
    """Write a REAL git repository fixture: many files with broadly-
    matching lowercase-word content, so a bare [a-z]{4,} pattern matches
    nearly every line -- the exact "no fixed literal substring" pattern
    class the issue's incidents were built around. A real ``git init``
    lets _resolve_repo_path's own "full path + is-git-repo" branch resolve
    this repo directly, with no registry/alias mocking needed at all.
    """
    repo_path.mkdir(parents=True, exist_ok=True)
    for i in range(_FIXTURE_FILE_COUNT):
        lines = [
            f"def function_{i}_{j}(argument_name, another_argument):"
            for j in range(_FIXTURE_LINES_PER_FILE)
        ]
        (repo_path / f"module_{i}.py").write_text("\n".join(lines) + "\n")
    subprocess.run(
        ["git", "init", "--quiet"], cwd=str(repo_path), check=True, timeout=10
    )


def _generate_real_ceiling_fixture_repo(repo_path) -> None:
    """A single large generated file whose real rg --json output
    genuinely exceeds the REAL (unpatched) 64 MiB ceiling."""
    repo_path.mkdir(parents=True, exist_ok=True)
    lines = [
        f"def function_{j}(argument_name, another_argument):"
        for j in range(_REAL_CEILING_LINE_COUNT)
    ]
    (repo_path / "big_module.py").write_text("\n".join(lines) + "\n")
    subprocess.run(
        ["git", "init", "--quiet"], cwd=str(repo_path), check=True, timeout=10
    )


def _assert_pattern_genuinely_exceeds_ceiling(
    repo_path, pattern: str, ceiling: int
) -> None:
    """AC-B1: empirically prove this pattern/fixture combination produces
    output exceeding ``ceiling`` via a real rg subprocess run directly."""
    probe = subprocess.run(
        ["rg", "--json", "-e", pattern, "--", str(repo_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, (
        f"probe rg invocation failed unexpectedly: exit={probe.returncode} "
        f"stderr={probe.stderr!r}"
    )
    assert len(probe.stdout.encode("utf-8")) > ceiling, (
        "fixture/pattern combination must genuinely exceed the ceiling "
        "for this to be a discriminating test"
    )


def _enter_real_state(tmp_path):
    """Set app.state.golden_repos_dir and install a REAL ConfigService
    (file-only, no DB pool configured). Returns
    (previous_golden_dir_or_MISSING, previous_config_service_or_None) for
    ``_exit_real_state`` to restore exactly. Caller must hold
    ``_GLOBAL_STATE_LOCK``.
    """
    previous_golden_dir = getattr(app_module.app.state, "golden_repos_dir", _MISSING)
    app_module.app.state.golden_repos_dir = str(tmp_path)
    previous_config_service = config_service_module._config_service
    real_service = ConfigService(server_dir_path=str(tmp_path / "cfgdir"))
    set_config_service(real_service)
    return previous_golden_dir, previous_config_service


def _exit_real_state(previous_golden_dir, previous_config_service) -> None:
    """Restore exactly what ``_enter_real_state`` captured -- including
    removing the golden_repos_dir attribute entirely if it was absent
    before, rather than leaking a spurious None. Caller must hold
    ``_GLOBAL_STATE_LOCK``.
    """
    if previous_golden_dir is _MISSING:
        try:
            delattr(app_module.app.state, "golden_repos_dir")
        except AttributeError:
            pass
    else:
        app_module.app.state.golden_repos_dir = previous_golden_dir

    if previous_config_service is not None:
        set_config_service(previous_config_service)
    else:
        reset_config_service()


@asynccontextmanager
async def _real_bootstrap(tmp_path):
    """Async-lock-guarded, real (non-mocked) bootstrap for tests that do
    NOT also need tempdir isolation (see ``_real_ceiling_bootstrap`` for
    the one that does -- asyncio.Lock is not reentrant, so the two are
    never composed together)."""
    async with _GLOBAL_STATE_LOCK:
        previous_golden_dir, previous_config_service = _enter_real_state(tmp_path)
        try:
            yield
        finally:
            _exit_real_state(previous_golden_dir, previous_config_service)


@asynccontextmanager
async def _real_ceiling_bootstrap(tmp_path):
    """Combined async-lock-guarded bootstrap for the real-ceiling E2E
    test ONLY: acquires ``_GLOBAL_STATE_LOCK`` exactly ONCE and adds
    ``tempfile.tempdir`` isolation (the documented, sanctioned CPython
    mechanism for changing where ``mkstemp()`` writes -- not a mock/patch
    of any function) on top of ``_enter_real_state``'s golden-dir/config
    setup, so the polling observer only ever sees THIS test's own
    subprocess output, never another concurrently-running process's temp
    files.

    Yields the isolated temp directory path.
    """
    async with _GLOBAL_STATE_LOCK:
        previous_golden_dir, previous_config_service = _enter_real_state(tmp_path)
        isolated_dir = tmp_path / "isolated_system_tmp"
        isolated_dir.mkdir(parents=True, exist_ok=True)
        previous_tempdir = tempfile.tempdir
        tempfile.tempdir = str(isolated_dir)
        try:
            yield str(isolated_dir)
        finally:
            tempfile.tempdir = previous_tempdir
            _exit_real_state(previous_golden_dir, previous_config_service)


async def _run_tracking_temp_file_size(coro, tmpdir: str):
    """Run ``coro`` (bounded by ``_POLL_DEADLINE_SECONDS`` via
    ``asyncio.wait_for``, so a genuine hang raises cleanly) while
    concurrently, and purely observationally (real filesystem polling --
    no mocking of any kind), watching ``tmpdir`` for this search's own
    rg_search_*/grep_search_* output file and recording its maximum
    observed real on-disk size.

    Returns (result, observed) where observed = {"seen": bool, "bytes": int}.
    """
    observed = {"seen": False, "bytes": 0}
    done = asyncio.Event()

    async def _poll() -> None:
        while not done.is_set():
            paths = glob.glob(os.path.join(tmpdir, "rg_search_*")) + glob.glob(
                os.path.join(tmpdir, "grep_search_*")
            )
            for path in paths:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                observed["seen"] = True
                if size > observed["bytes"]:
                    observed["bytes"] = size
            await asyncio.sleep(_TEMP_FILE_POLL_INTERVAL_SECONDS)

    poll_task = asyncio.create_task(_poll())
    try:
        result = await asyncio.wait_for(coro, timeout=_POLL_DEADLINE_SECONDS)
    finally:
        done.set()
        await poll_task
    return result, observed


@pytest.mark.slow
class TestRegexSearchIntegrationBoundedReadSmallCeiling:
    """Integration-tier (honestly NOT labeled E2E): real rg/RegexSearchService
    behavior against a DELIBERATELY patched, small ``_MAX_READ_BYTES``
    ceiling for fast execution -- proves the mechanism's correctness, not
    the real production ceiling (see ``TestRegexSearchE2EBoundedReadRealCeiling``
    below for that)."""

    @pytest.mark.asyncio
    async def test_broad_pattern_against_real_repo_stays_bounded(self, tmp_path):
        import code_indexer.global_repos.regex_search as regex_search_module

        repo_path = tmp_path / "fixture-repo"
        _generate_fixture_repo(repo_path)
        _assert_pattern_genuinely_exceeds_ceiling(
            repo_path, _BROAD_PATTERN, _TEST_BYTE_CEILING
        )

        # Full absolute path, no "-global" suffix: resolved by
        # _resolve_repo_path's real is-git-repo branch, no mocking.
        args = {"repository_alias": str(repo_path), "pattern": _BROAD_PATTERN}
        no_match_args = {
            "repository_alias": str(repo_path),
            "pattern": "NOTHING_MATCHES_THIS_LITERAL_TOKEN",
        }

        with patch.object(regex_search_module, "_MAX_READ_BYTES", _TEST_BYTE_CEILING):
            async with _real_bootstrap(tmp_path):
                start = time.monotonic()
                result = await handle_regex_search(args, _make_user())
                # AC-B6: the server remains responsive immediately after --
                # a follow-up call succeeds without hanging or crashing.
                follow_up = await handle_regex_search(no_match_args, _make_user())
                elapsed_seconds = time.monotonic() - start

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert data["read_capped"] is True  # AC-B5
        assert elapsed_seconds < 30, f"took {elapsed_seconds:.1f}s, expected <30s"

        follow_up_data = json.loads(follow_up["content"][0]["text"])
        assert follow_up_data["success"] is True
        assert follow_up_data["matches"] == []


@pytest.mark.slow
class TestRegexSearchE2EBoundedReadRealCeiling:
    """Genuinely zero-mock E2E: real ``rg`` subprocess, real repo
    resolution, the REAL unpatched 64 MiB production ceiling, and direct
    byte-count evidence via real filesystem observation -- no
    ``unittest.mock`` usage anywhere in this class."""

    @pytest.mark.asyncio
    async def test_broad_pattern_against_real_repo_stays_bounded_at_real_production_ceiling(
        self, tmp_path
    ):
        repo_path = tmp_path / "fixture-repo-real-ceiling"
        _generate_real_ceiling_fixture_repo(repo_path)
        _assert_pattern_genuinely_exceeds_ceiling(
            repo_path, _BROAD_PATTERN, _REAL_MAX_READ_BYTES
        )

        args = {"repository_alias": str(repo_path), "pattern": _BROAD_PATTERN}

        async with _real_ceiling_bootstrap(tmp_path) as isolated_tmpdir:
            start = time.monotonic()
            result, observed = await _run_tracking_temp_file_size(
                handle_regex_search(args, _make_user()), isolated_tmpdir
            )
            elapsed_seconds = time.monotonic() - start

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert data["read_capped"] is True
        assert elapsed_seconds < 60, f"took {elapsed_seconds:.1f}s, expected <60s"

        assert observed["seen"], "no temp search output file was ever observed"
        assert observed["bytes"] <= _REAL_MAX_READ_BYTES + _POLL_SIZE_SLACK_BYTES, (
            f"observed temp output file size {observed['bytes']} bytes "
            f"exceeded the real {_REAL_MAX_READ_BYTES}-byte ceiling by "
            f"more than the allowed slack"
        )
        # Issue #1601 remediation round 4 (Priority 5 -- Codex Medium): an
        # upper-bound-only assertion would ALSO pass if _MAX_READ_BYTES
        # were misconfigured down to something far smaller (e.g. 16 KiB) --
        # it only proves "not unbounded", not "actually near the real
        # production ceiling". Assert a lower bound too: the observed size
        # must be within _MIN_OBSERVED_FRACTION_OF_REAL_CEILING of the real
        # ceiling. This generous (not exact-equality) floor tolerates real,
        # expected slack sources -- Python's default buffered writer on
        # the output file lags the true byte count until a flush/close,
        # and the polling loop's 2ms interval can miss the exact peak
        # before cleanup deletes the file -- while still failing hard
        # against an order-of-magnitude-smaller misconfigured ceiling.
        min_expected_bytes = int(
            _REAL_MAX_READ_BYTES * _MIN_OBSERVED_FRACTION_OF_REAL_CEILING
        )
        assert observed["bytes"] >= min_expected_bytes, (
            f"observed temp output file size {observed['bytes']} bytes is "
            f"far below the real {_REAL_MAX_READ_BYTES}-byte ceiling "
            f"(expected at least {min_expected_bytes} bytes) -- this test "
            f"would also pass if _MAX_READ_BYTES were misconfigured down "
            f"to a much smaller value, which is exactly what this bound "
            f"exists to catch"
        )

    @pytest.mark.asyncio
    async def test_zero_matches_fast_path_stays_fast_and_correct(self, tmp_path):
        """AC-B8: a pattern matching nothing returns quickly and correctly
        -- the fast path this fix must not regress."""
        repo_path = tmp_path / "fixture-repo-zero"
        _generate_fixture_repo(repo_path)
        args = {
            "repository_alias": str(repo_path),
            "pattern": "ZZZZZZ_THIS_LITERAL_NEVER_APPEARS_ANYWHERE",
        }

        async with _real_bootstrap(tmp_path):
            start = time.monotonic()
            result = await handle_regex_search(args, _make_user())
            elapsed_seconds = time.monotonic() - start

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert data["matches"] == []
        assert data["total_matches"] == 0
        assert data["read_capped"] is False
        assert elapsed_seconds < 2.0, (
            f"zero-match fast path took {elapsed_seconds:.3f}s, expected <2.0s"
        )
