"""Issue #1601 remediation Priority 4: AC-G1 real omni (multi-repo) E2E.

Both independent code reviewers found the original AC-G1 substitution
(``test_regex_search_omni_read_capped_1601.py``) inadequate: it only proves
boolean-OR aggregation logic via full mocking of ``handle_regex_search``
itself, exercising none of real repository resolution, real subprocesses,
temp-file lifecycle across repos, or aggregate-request memory-boundedness.

This test drives the real ``_omni_regex_search`` -> ``handle_regex_search``
path across TWO real git-fixture repos with NO mocking and NO monkeypatch
of any internal:

- The REAL 64 MiB ``_MAX_READ_BYTES`` production ceiling is exercised
  directly -- one fixture repo contains a single generated file large
  enough that ripgrep's real ``--json`` output genuinely exceeds 64 MiB
  (empirically proven inline, AC-B1-style, via a real ``rg`` probe run
  before the actual call), rather than patching the constant down.
  Benchmark: 300,000 matching lines produced ~124 MiB of real rg JSON
  output in well under a second, so a several-hundred-thousand-line
  fixture keeps this fast while genuinely crossing the real ceiling.
- The config service is a REAL ``ConfigService`` instance backed by a real
  (temporary) server directory -- installed as the process-wide singleton
  via ``set_config_service``/``reset_config_service`` (the module's own
  documented test seam for exactly this: "Intended for integration tests
  that need the real singleton wired to a specific server directory
  without process-level mocking"). ``ConfigService.load_config()`` never
  touches a database when no runtime DB pool/SQLite path has been
  configured (the case here), so this stays lightweight -- no DB
  bootstrap required, and every config value (timeouts, worker counts,
  omni caps) is genuinely the dataclass-default ``ServerConfig``.
- ``app.state.golden_repos_dir`` is set directly on the real FastAPI app
  singleton, guarded by a module-level lock and try/finally restored, so
  concurrent execution of this test cannot race another test mutating the
  same process-wide singleton state.

Repository-path resolution, ripgrep subprocess execution, and file I/O are
all real. Cleanup/reaping are verified via real observable state: a
before/after temp-directory diff and real psutil process-tree inspection.

Slow tier: excluded from fast-automation.sh's default run via
@pytest.mark.slow, matching the single-repo E2E test's own tier.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import tempfile
import threading
import time

import psutil
import pytest

from code_indexer.server import app as app_module
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.search import handle_regex_search
from code_indexer.server.services.config_service import (
    ConfigService,
    reset_config_service,
    set_config_service,
)

# ~200,000 matching lines of real rg --json output comfortably exceeds the
# real 64 MiB _MAX_READ_BYTES ceiling (benchmarked: 300,000 such lines
# produced ~124 MiB in well under a second).
_CAPPED_REPO_LINE_COUNT = 200_000
_SMALL_REPO_FILE_COUNT = 5
_SMALL_REPO_LINES_PER_FILE = 5
_BROAD_PATTERN = r"[a-z]{4,}"  # matches almost every generated line
_REAL_MAX_READ_BYTES = 64 * 1024 * 1024

_GIT_INIT_TIMEOUT_SECONDS = 10
_RG_PROBE_TIMEOUT_SECONDS = 30
_MAX_SEARCH_DURATION_SECONDS = 60

# Not a real credential -- a literal placeholder satisfying the User
# dataclass's required password_hash field for this in-process call; never
# checked against anything.
_TEST_USER_HASH_LITERAL = "test-literal-unused-for-auth-not-a-credential"

# Guards mutation of the process-wide ConfigService singleton and the real
# FastAPI app's golden_repos_dir state, so concurrent test execution cannot
# race another test touching the same global state.
_APP_STATE_LOCK = threading.Lock()


def _make_user() -> User:
    from datetime import datetime, timezone

    return User(
        username="e2e_omni_test_user",
        password_hash=_TEST_USER_HASH_LITERAL,
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _initialize_git_repo(repo_path) -> None:
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(repo_path),
        check=True,
        timeout=_GIT_INIT_TIMEOUT_SECONDS,
    )


def _generate_capped_repo(repo_path) -> None:
    """A single large file whose real rg --json output genuinely exceeds
    the real 64 MiB ceiling, plus a real `git init`."""
    repo_path.mkdir(parents=True, exist_ok=True)
    lines = [
        f"def function_{j}(argument_name, another_argument):"
        for j in range(_CAPPED_REPO_LINE_COUNT)
    ]
    (repo_path / "big_module.py").write_text("\n".join(lines) + "\n")
    _initialize_git_repo(repo_path)


def _generate_small_repo(repo_path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    for i in range(_SMALL_REPO_FILE_COUNT):
        lines = [
            f"def small_function_{i}_{j}(argument_name, another_argument):"
            for j in range(_SMALL_REPO_LINES_PER_FILE)
        ]
        (repo_path / f"module_{i}.py").write_text("\n".join(lines) + "\n")
    _initialize_git_repo(repo_path)


def _assert_pattern_exceeds_real_ceiling(repo_path) -> None:
    """AC-B1-style empirical proof: this fixture genuinely exceeds the
    REAL 64 MiB ceiling via a real rg run -- no patched constant."""
    probe = subprocess.run(
        ["rg", "--json", "-e", _BROAD_PATTERN, "--", str(repo_path)],
        capture_output=True,
        text=True,
        timeout=_RG_PROBE_TIMEOUT_SECONDS,
    )
    assert probe.returncode == 0, (
        f"probe rg invocation failed unexpectedly: exit={probe.returncode} "
        f"stderr={probe.stderr!r}"
    )
    assert len(probe.stdout.encode("utf-8")) > _REAL_MAX_READ_BYTES


def _rg_temp_snapshot() -> set:
    """Real directory listing of leftover rg/grep search temp files in the
    real system temp directory -- observational, no mocking."""
    tmpdir = tempfile.gettempdir()
    return set(glob.glob(os.path.join(tmpdir, "rg_search_*"))) | set(
        glob.glob(os.path.join(tmpdir, "grep_search_*"))
    )


async def _execute_omni_search_with_real_bootstrap(tmp_path, capped_repo, small_repo):
    """Real config bootstrap + real (lock-guarded, try/finally restored)
    app-state mutation around the actual handle_regex_search call.

    Returns (data, elapsed_seconds, temp_before, current_process).
    """
    real_config_service = ConfigService(server_dir_path=str(tmp_path / "cfgdir"))
    with _APP_STATE_LOCK:
        set_config_service(real_config_service)
        previous_golden_dir = getattr(app_module.app.state, "golden_repos_dir", None)
        app_module.app.state.golden_repos_dir = str(tmp_path)
        try:
            temp_before = _rg_temp_snapshot()
            current_process = psutil.Process(os.getpid())
            args = {
                "repository_alias": [str(capped_repo), str(small_repo)],
                "pattern": _BROAD_PATTERN,
            }
            start = time.monotonic()
            result = await handle_regex_search(args, _make_user())
            elapsed_seconds = time.monotonic() - start
        finally:
            app_module.app.state.golden_repos_dir = previous_golden_dir
            reset_config_service()

    data = json.loads(result["content"][0]["text"])
    return data, elapsed_seconds, temp_before, current_process


def _assert_omni_response(data, elapsed_seconds, capped_repo, small_repo) -> None:
    assert data["success"] is True
    assert data["repos_searched"] == 2
    assert data["errors"] == {}
    assert elapsed_seconds < _MAX_SEARCH_DURATION_SECONDS, (
        f"took {elapsed_seconds:.1f}s, expected <{_MAX_SEARCH_DURATION_SECONDS}s"
    )
    # OR-aggregated across the real multi-repo response: capped_repo was
    # genuinely capped, small_repo was not.
    assert data["read_capped"] is True
    source_repos = {m["source_repo"] for m in data["matches"]}
    assert str(capped_repo) in source_repos
    assert str(small_repo) in source_repos


def _assert_cleanup_and_reaping(temp_before, current_process) -> None:
    leaked = _rg_temp_snapshot() - temp_before
    assert not leaked, f"temp files leaked after the omni request: {leaked}"

    remaining_children = current_process.children(recursive=True)
    zombies = [c for c in remaining_children if c.status() == psutil.STATUS_ZOMBIE]
    assert not zombies, f"zombie child processes left behind: {zombies}"


@pytest.mark.slow
class TestRegexSearchOmniE2ETwoRepos:
    """Priority 4 (AC-G1): real, unmocked omni regex_search across two
    real repositories -- proves the aggregate behavior a single-repo test
    cannot."""

    @pytest.mark.asyncio
    async def test_omni_search_across_two_real_repos_aggregates_correctly(
        self, tmp_path
    ):
        capped_repo = tmp_path / "capped-repo"
        small_repo = tmp_path / "small-repo"
        _generate_capped_repo(capped_repo)
        _generate_small_repo(small_repo)
        _assert_pattern_exceeds_real_ceiling(capped_repo)

        (
            data,
            elapsed_seconds,
            temp_before,
            current_process,
        ) = await _execute_omni_search_with_real_bootstrap(
            tmp_path, capped_repo, small_repo
        )

        _assert_omni_response(data, elapsed_seconds, capped_repo, small_repo)
        _assert_cleanup_and_reaping(temp_before, current_process)
