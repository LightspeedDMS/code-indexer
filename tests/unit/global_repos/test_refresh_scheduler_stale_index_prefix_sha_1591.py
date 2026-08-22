"""
Unit tests for Bug #1591: stale-index drift check compares an abbreviated
recorded SHA against the full `git rev-parse HEAD`, forcing an unnecessary
full reconcile on every refresh forever for repos whose metadata.json was
last written by config_fixer.py's GitStateDetector (which used
`git rev-parse --short HEAD`, or the literal "unknown" on failure).

RefreshScheduler._check_stale_index_metadata() (Bug #1508) compares
metadata.json's recorded `current_commit` against the actual working-tree
HEAD with plain string equality. Two independent producers disagree on
format:
  - git_topology_service.py's _get_current_commit() -> full 40-char SHA
    (the normal indexing path).
  - config_fixer.py's GitStateDetector.detect_git_state() -> 7-char SHA
    (or "unknown" on failure).

A 7-char recorded SHA that is genuinely a *prefix* of the actual 40-char
HEAD means the index is NOT drifted -- but the old exact-string-equality
check can never recognize this, so it force-reconciles on every single
refresh cycle, forever, even when the index is exactly current.

These tests exercise the REAL `_check_stale_index_metadata()` method
against a REAL local git repository (real `git init`/`git commit`
subprocess calls, real `git rev-parse HEAD` executed by the method under
test) -- no mocking of git itself.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_git_repo():
    """Create a real, temporary git repository with one commit."""
    repo_dir = Path(tempfile.mkdtemp(prefix="test_stale_index_prefix_sha_1591_"))
    try:
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        (repo_dir / "file1.txt").write_text("content1")
        subprocess.run(
            ["git", "add", "."], cwd=repo_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        yield repo_dir
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


@pytest.fixture
def real_git_repo_two_commits():
    """Create a real, temporary git repository with TWO commits, so tests
    can record a genuinely STALE (but real, valid) commit SHA against a
    working tree whose HEAD has since moved on -- real drift, not a
    synthetic hand-rolled string."""
    repo_dir = Path(tempfile.mkdtemp(prefix="test_stale_index_two_commits_1591_"))
    try:
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        (repo_dir / "file1.txt").write_text("content1")
        subprocess.run(
            ["git", "add", "."], cwd=repo_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        first_commit = _actual_head(repo_dir)

        (repo_dir / "file2.txt").write_text("content2")
        subprocess.run(
            ["git", "add", "."], cwd=repo_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Second commit"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        second_commit = _actual_head(repo_dir)

        yield repo_dir, first_commit, second_commit
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def _actual_head(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_metadata(source_path: Path, filename: str = "metadata.json", **fields):
    """Write a metadata JSON file under .code-indexer/.

    filename defaults to the legacy bare "metadata.json". Pass
    filename="metadata-voyage-ai.json" to write the REAL production
    filename SmartIndexer uses for the default embedding provider --
    confirmed via a live census of the dev golden-repos directory: 15
    metadata-voyage-ai.json, 13 metadata-cohere.json, only 2 bare legacy
    metadata.json. Bug #1591's provider-aware fix must read THIS file,
    not just the legacy one.
    """
    meta_dir = source_path / ".code-indexer"
    meta_dir.mkdir(parents=True, exist_ok=True)
    with open(meta_dir / filename, "w") as f:
        json.dump(fields, f)


@pytest.fixture
def golden_repos_dir(tmp_path):
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def mock_query_tracker():
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return config


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.get_global_repo.return_value = {
        "alias_name": "my-repo-global",
        "repo_url": "git@github.com:org/my-repo.git",
        "default_branch": "main",
    }
    registry.list_global_repos.return_value = []
    registry.update_refresh_timestamp.return_value = None
    return registry


@pytest.fixture
def scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


# ---------------------------------------------------------------------------
# RED: a valid abbreviated-SHA prefix of the actual HEAD must NOT be
# reported as drift.
# ---------------------------------------------------------------------------


class TestAbbreviatedShaPrefixIsNotDrift:
    def test_seven_char_prefix_of_actual_head_is_not_reported_as_drift(
        self, scheduler, real_git_repo
    ):
        """Reproduces the exact production scenario from Issue #1591:
        email-marketing-api-global recorded 'a98920c' while actual HEAD was
        'a98920c5ac4d2ebd1e844f33643d09286a7329e4' -- same commit, index is
        genuinely current, must NOT force a reconcile."""
        actual_head = _actual_head(real_git_repo)
        abbreviated = actual_head[:7]

        _write_metadata(
            real_git_repo,
            status="completed",
            current_commit=abbreviated,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "email-marketing-api-global"
        )

        assert result is False, (
            f"A recorded 7-char prefix ({abbreviated!r}) of the actual HEAD "
            f"({actual_head!r}) is the SAME commit -- this must not force a "
            "reconcile (Bug #1591)."
        )

    def test_eight_char_prefix_of_actual_head_is_not_reported_as_drift(
        self, scheduler, real_git_repo
    ):
        actual_head = _actual_head(real_git_repo)
        abbreviated = actual_head[:8]

        _write_metadata(
            real_git_repo,
            status="completed",
            current_commit=abbreviated,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "some-repo-global"
        )

        assert result is False

    def test_full_sha_exact_match_still_not_reported_as_drift(
        self, scheduler, real_git_repo
    ):
        """Regression guard: the original exact-match (full SHA vs full SHA)
        behavior must continue to work."""
        actual_head = _actual_head(real_git_repo)

        _write_metadata(
            real_git_repo,
            status="completed",
            current_commit=actual_head,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "some-repo-global"
        )

        assert result is False


# ---------------------------------------------------------------------------
# Negative case: a recorded short SHA that is NOT a prefix of the actual
# HEAD is genuine drift and must still be reported as such.
# ---------------------------------------------------------------------------


class TestGenuineDriftStillDetected:
    def test_short_sha_not_matching_actual_head_is_genuine_drift(
        self, scheduler, real_git_repo
    ):
        actual_head = _actual_head(real_git_repo)
        # Construct a 7-char value that is NOT a prefix of actual_head by
        # flipping the first hex character to something different.
        first_char = actual_head[0]
        replacement = "0" if first_char != "0" else "1"
        not_a_prefix = replacement + actual_head[1:7]
        assert not actual_head.startswith(not_a_prefix)

        _write_metadata(
            real_git_repo,
            status="completed",
            current_commit=not_a_prefix,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "some-repo-global"
        )

        assert result is True, (
            "A recorded short SHA that genuinely does NOT match the actual "
            "HEAD's prefix must still be reported as drift -- the fix must "
            "not make real drift undetectable (Bug #1591 negative case)."
        )

    def test_full_sha_mismatch_is_genuine_drift(self, scheduler, real_git_repo):
        """Regression guard: a full 40-char SHA that legitimately differs
        from HEAD (e.g. before a pull) must still force reconcile."""
        recorded_full_sha = "bbbb00000000000000000000000000000000bbbb"
        assert len(recorded_full_sha) == 40, "fixture must be a real 40-char SHA"
        _write_metadata(
            real_git_repo,
            status="completed",
            current_commit=recorded_full_sha,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "some-repo-global"
        )

        assert result is True


# ---------------------------------------------------------------------------
# The "unknown" sentinel (config_fixer.py's failure-path literal) carries no
# usable commit information -- it must force a reconcile (not be treated as
# a SHA that can never match, but also not silently ignored forever).
# ---------------------------------------------------------------------------


class TestUnknownSentinelForcesReconcile:
    def test_unknown_literal_recorded_commit_forces_reconcile(
        self, scheduler, real_git_repo
    ):
        _write_metadata(
            real_git_repo,
            status="completed",
            current_commit="unknown",
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "cidx-meta-global"
        )

        assert result is True, (
            "The literal 'unknown' sentinel carries no usable commit "
            "information and must force a reconcile so a real commit gets "
            "recorded going forward (Bug #1591)."
        )


# ---------------------------------------------------------------------------
# BLOCKING code-review fix: the reader must be provider-aware. SmartIndexer
# writes .code-indexer/metadata-{provider}.json (e.g. metadata-voyage-ai.json)
# in production, NOT the bare legacy metadata.json this method originally
# read exclusively. A live census of the dev golden-repos directory found
# 15 metadata-voyage-ai.json + 13 metadata-cohere.json vs only 2 bare
# metadata.json -- the drift check was inert on ~93% of the fleet. These
# tests reproduce that gap against a REAL git repo and prove the fix (reuse
# of server/services/metadata_reader.py's read_current_commit(), which
# already implements provider-first-then-legacy-fallback resolution).
# ---------------------------------------------------------------------------


class TestProviderAwareMetadataRead:
    def test_provider_suffixed_metadata_prefix_match_is_not_drift(
        self, scheduler, real_git_repo
    ):
        actual_head = _actual_head(real_git_repo)
        abbreviated = actual_head[:7]

        _write_metadata(
            real_git_repo,
            filename="metadata-voyage-ai.json",
            status="completed",
            current_commit=abbreviated,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "some-repo-global"
        )

        assert result is False, (
            "A prefix match recorded in the REAL production provider-"
            "suffixed metadata file must not force a reconcile."
        )

    def test_provider_suffixed_metadata_genuine_drift_is_detected(
        self, scheduler, real_git_repo_two_commits
    ):
        """The blocking fix: before the fix, this method only ever opened
        the bare legacy metadata.json and returned False immediately when
        that file was absent -- so a genuinely stale current_commit
        recorded ONLY in metadata-voyage-ai.json was silently ignored
        forever, masking real drift on ~93% of the fleet."""
        repo_dir, first_commit, second_commit = real_git_repo_two_commits

        _write_metadata(
            repo_dir,
            filename="metadata-voyage-ai.json",
            status="completed",
            current_commit=first_commit,
        )

        result = scheduler._check_stale_index_metadata(
            str(repo_dir), "some-repo-global"
        )

        assert result is True, (
            "A genuinely stale current_commit recorded in the provider-"
            "suffixed metadata file (the file SmartIndexer actually "
            "writes in production) must be detected as drift -- the "
            "reader must not silently ignore it just because the legacy "
            "bare metadata.json does not exist (Bug #1591 blocking fix)."
        )

    def test_provider_suffixed_metadata_unknown_forces_reconcile(
        self, scheduler, real_git_repo
    ):
        _write_metadata(
            real_git_repo,
            filename="metadata-voyage-ai.json",
            status="completed",
            current_commit="unknown",
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "cidx-meta-global"
        )

        assert result is True, (
            "'unknown' recorded in the provider-suffixed metadata file "
            "must still force a reconcile -- the provider-aware read must "
            "not accidentally suppress this signal either."
        )

    def test_provider_file_takes_precedence_over_stale_legacy_file(
        self, scheduler, real_git_repo_two_commits
    ):
        """Precedence guard matching metadata_reader.read_current_commit's
        documented contract: the provider file is authoritative when both
        exist, even if the legacy file looks fresher."""
        repo_dir, first_commit, second_commit = real_git_repo_two_commits

        # Legacy file claims the index is fully current (matches HEAD).
        _write_metadata(
            repo_dir,
            filename="metadata.json",
            status="completed",
            current_commit=second_commit,
        )
        # Provider file (the one actually consulted by real indexing)
        # records the genuinely stale first commit.
        _write_metadata(
            repo_dir,
            filename="metadata-voyage-ai.json",
            status="completed",
            current_commit=first_commit,
        )

        result = scheduler._check_stale_index_metadata(
            str(repo_dir), "some-repo-global"
        )

        assert result is True, (
            "When both a provider-suffixed and a legacy metadata file "
            "exist, the provider file must take precedence (matching "
            "read_current_commit's contract) -- a stale provider file "
            "must not be masked by a stale-but-unused legacy file."
        )


# ---------------------------------------------------------------------------
# Minimum prefix length floor: any hex fragment, however short, was being
# accepted as a valid "prefix match" -- e.g. a single character. Git's own
# default abbreviation length is 7; anything shorter carries a real
# collision risk and must not suppress drift detection.
# ---------------------------------------------------------------------------


class TestMinimumPrefixLengthFloor:
    def test_single_char_prefix_of_actual_head_forces_reconcile(
        self, scheduler, real_git_repo
    ):
        actual_head = _actual_head(real_git_repo)
        too_short = actual_head[:1]

        _write_metadata(
            real_git_repo,
            status="completed",
            current_commit=too_short,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "some-repo-global"
        )

        assert result is True, (
            f"A 1-char recorded fragment ({too_short!r}) is technically a "
            "prefix of the actual HEAD but far too short to trust as a "
            "real commit identifier -- it must not suppress drift "
            "detection (minimum length floor of 7, matching git's own "
            "default abbreviation length)."
        )

    def test_six_char_prefix_of_actual_head_forces_reconcile(
        self, scheduler, real_git_repo
    ):
        actual_head = _actual_head(real_git_repo)
        too_short = actual_head[:6]

        _write_metadata(
            real_git_repo,
            status="completed",
            current_commit=too_short,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "some-repo-global"
        )

        assert result is True, (
            "A 6-char recorded fragment is one below the 7-char minimum "
            "floor and must still force a reconcile."
        )


# ---------------------------------------------------------------------------
# "unknown" comparison must be normalized (case/whitespace) exactly like
# the hex-fragment comparison, so "UNKNOWN" or " unknown " don't fall
# through to the generic drift branch and log a nonsensical
# "reflects commit UNKNOWN" message.
# ---------------------------------------------------------------------------


class TestUnknownCaseInsensitiveNormalization:
    def test_uppercase_unknown_forces_reconcile_with_correct_message(
        self, scheduler, real_git_repo, caplog
    ):
        _write_metadata(
            real_git_repo,
            status="completed",
            current_commit="UNKNOWN",
        )

        with caplog.at_level("WARNING"):
            result = scheduler._check_stale_index_metadata(
                str(real_git_repo), "cidx-meta-global"
            )

        assert result is True
        messages = " ".join(r.message for r in caplog.records)
        assert "no usable recorded commit" in messages, (
            "'UNKNOWN' (uppercase) must be recognized as the unknown "
            "sentinel and logged via the dedicated message, not fall "
            f"through to the generic drift-mismatch branch. Got: {messages!r}"
        )
        assert "reflects commit UNKNOWN" not in messages

    def test_whitespace_padded_unknown_forces_reconcile_with_correct_message(
        self, scheduler, real_git_repo, caplog
    ):
        _write_metadata(
            real_git_repo,
            status="completed",
            current_commit=" unknown \n",
        )

        with caplog.at_level("WARNING"):
            result = scheduler._check_stale_index_metadata(
                str(real_git_repo), "cidx-meta-global"
            )

        assert result is True
        messages = " ".join(r.message for r in caplog.records)
        assert "no usable recorded commit" in messages, (
            "' unknown \\n' (whitespace-padded) must be recognized as the "
            f"unknown sentinel via the dedicated message. Got: {messages!r}"
        )


# ---------------------------------------------------------------------------
# Real two-commit coverage (per code review): uppercase recorded SHA and
# recorded-longer-than-HEAD, exercised against REAL git commit data rather
# than synthetic hand-rolled strings.
# ---------------------------------------------------------------------------


class TestGenuineDriftRealCommitCoverage:
    def test_uppercase_prefix_of_actual_head_is_not_drift(
        self, scheduler, real_git_repo_two_commits
    ):
        repo_dir, _first_commit, second_commit = real_git_repo_two_commits
        uppercase_prefix = second_commit[:8].upper()

        _write_metadata(
            repo_dir,
            status="completed",
            current_commit=uppercase_prefix,
        )

        result = scheduler._check_stale_index_metadata(
            str(repo_dir), "some-repo-global"
        )

        assert result is False, (
            "An uppercase recorded prefix of the CURRENT real HEAD must "
            "still be recognized as a match (case-insensitive comparison)."
        )

    def test_uppercase_recorded_sha_of_stale_real_commit_is_still_drift(
        self, scheduler, real_git_repo_two_commits
    ):
        """Case-insensitivity must not accidentally let real drift through:
        this records a real, valid, but STALE commit (uppercased) while
        HEAD has genuinely moved to a second real commit."""
        repo_dir, first_commit, _second_commit = real_git_repo_two_commits
        uppercase_stale_prefix = first_commit[:8].upper()

        _write_metadata(
            repo_dir,
            status="completed",
            current_commit=uppercase_stale_prefix,
        )

        result = scheduler._check_stale_index_metadata(
            str(repo_dir), "some-repo-global"
        )

        assert result is True, (
            "A real, valid, uppercase-recorded SHA prefix that genuinely "
            "belongs to a STALE (superseded) commit must still be "
            "detected as drift -- case-insensitivity must not mask real "
            "drift."
        )

    def test_recorded_longer_than_actual_head_is_drift(
        self, scheduler, real_git_repo_two_commits
    ):
        repo_dir, _first_commit, second_commit = real_git_repo_two_commits
        # A real full HEAD SHA plus one extra hex char: longer than any
        # real SHA could ever be a prefix of, so this can never be a
        # genuine prefix match -- must be reported as drift.
        longer_than_head = second_commit + "0"

        _write_metadata(
            repo_dir,
            status="completed",
            current_commit=longer_than_head,
        )

        result = scheduler._check_stale_index_metadata(
            str(repo_dir), "some-repo-global"
        )

        assert result is True, (
            "A recorded value LONGER than the actual HEAD can never be a "
            "genuine prefix of it and must be reported as drift, not "
            "silently accepted."
        )
