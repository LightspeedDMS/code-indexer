"""
Unit tests for Bug #849 fix — service layer: _DomainUpdateResult and retry loop.

When Claude correctly determines no changes are needed (FILE_UNCHANGED sentinel),
invoke_delta_merge_file returns _DELTA_NOOP.  The service must:
1. Return _DomainUpdateResult.NOOP from _update_domain_file (not WRITTEN or FAILED)
2. Break out of the retry loop on NOOP without retrying

Tests:
- test_domain_update_result_enum_shape
- test_update_domain_file_return_values[noop/failed/written]
- test_update_domain_file_noop_does_not_write_file
- test_update_affected_domains_retry_behavior[noop/failed/written]
"""

import subprocess as _subprocess
from unittest.mock import Mock, patch as _patch

import pytest

from code_indexer.global_repos.dependency_map_analyzer import _DELTA_NOOP
from code_indexer.server.services.dependency_map_service import (
    MAX_DOMAIN_RETRIES,
    DependencyMapService,
    _DomainUpdateResult,
)

# ---------------------------------------------------------------------------
# Module-level test constants
# ---------------------------------------------------------------------------

TEST_TIMEOUT_SECONDS = 60
TEST_MAX_TURNS = 5
TEST_VERSION_DIR = "v_20260101"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    """DependencyMapService with mocked collaborators; golden_repos_dir = tmp_path."""
    gm = Mock()
    gm.golden_repos_dir = str(tmp_path)
    tracking = Mock()
    tracking.get_tracking.return_value = {"status": "pending", "commit_hashes": None}
    service = DependencyMapService(gm, Mock(), tracking, Mock())
    service._activity_journal = Mock()
    service._activity_journal.log = Mock()
    return service


@pytest.fixture
def config():
    """Minimal config mock for _update_domain_file / _update_affected_domains."""
    cfg = Mock()
    cfg.dependency_map_pass_timeout_seconds = TEST_TIMEOUT_SECONDS
    cfg.dependency_map_delta_max_turns = TEST_MAX_TURNS
    cfg.dep_map_fact_check_enabled = False
    return cfg


@pytest.fixture
def domain_file(tmp_path):
    """Minimal domain .md file with YAML frontmatter."""
    f = tmp_path / "auth.md"
    f.write_text("---\ndomain: auth\n---\n\n# Auth Domain\n\nContent.")
    return f


@pytest.fixture
def versioned_dep_map_dirs(tmp_path):
    """
    Create `.versioned/cidx-meta/<TEST_VERSION_DIR>/dependency-map/` so that
    `_get_cidx_meta_read_path()` resolves naturally from `tmp_path` (golden_repos_dir)
    without patching any service internals.

    Returns (dep_map_read_dir, dep_map_write_dir).
    """
    dep_map_read_dir = (
        tmp_path / ".versioned" / "cidx-meta" / TEST_VERSION_DIR / "dependency-map"
    )
    dep_map_read_dir.mkdir(parents=True)
    (dep_map_read_dir / "auth.md").write_text(
        "---\ndomain: auth\n---\n\n# Auth\n\nContent."
    )
    dep_map_write_dir = tmp_path / "dep-map-live"
    dep_map_write_dir.mkdir()
    (dep_map_write_dir / "auth.md").write_text(
        "---\ndomain: auth\n---\n\n# Auth\n\nContent."
    )
    return dep_map_read_dir, dep_map_write_dir


# ---------------------------------------------------------------------------
# Enum shape
# ---------------------------------------------------------------------------


def test_domain_update_result_enum_shape():
    """_DomainUpdateResult must have WRITTEN/NOOP/FAILED as three distinct members."""
    members = {
        _DomainUpdateResult.WRITTEN,
        _DomainUpdateResult.NOOP,
        _DomainUpdateResult.FAILED,
    }
    assert len(members) == 3, (
        "WRITTEN, NOOP, and FAILED must be three distinct enum values."
    )


# ---------------------------------------------------------------------------
# _update_domain_file return values (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invoke_return,expected_result",
    [
        (_DELTA_NOOP, _DomainUpdateResult.NOOP),
        (None, _DomainUpdateResult.FAILED),
        ("# Auth Domain\n\nUpdated content.", _DomainUpdateResult.WRITTEN),
    ],
    ids=["noop", "failed", "written"],
)
def test_update_domain_file_return_values(
    svc, config, domain_file, invoke_return, expected_result
):
    """_update_domain_file returns the correct _DomainUpdateResult for each analyzer outcome."""
    svc._analyzer.invoke_delta_merge_file.return_value = invoke_return
    svc._analyzer.build_delta_merge_prompt.return_value = "merge prompt"

    result = svc._update_domain_file(
        domain_name="auth",
        domain_file=domain_file,
        changed_repos=["repo1"],
        new_repos=[],
        removed_repos=[],
        domain_list=["auth", "billing"],
        config=config,
        read_file=domain_file,
    )

    assert result == expected_result, (
        f"Expected {expected_result!r} for invoke_return={invoke_return!r}, got {result!r}."
    )


# ---------------------------------------------------------------------------
# NOOP must not write the file
# ---------------------------------------------------------------------------


def test_update_domain_file_noop_does_not_write_file(svc, config, domain_file):
    """When _DELTA_NOOP returned, domain file content and mtime are preserved."""
    original_content = domain_file.read_text()
    original_mtime = domain_file.stat().st_mtime

    svc._analyzer.invoke_delta_merge_file.return_value = _DELTA_NOOP
    svc._analyzer.build_delta_merge_prompt.return_value = "merge prompt"

    svc._update_domain_file(
        domain_name="auth",
        domain_file=domain_file,
        changed_repos=["repo1"],
        new_repos=[],
        removed_repos=[],
        domain_list=["auth"],
        config=config,
        read_file=domain_file,
    )

    assert domain_file.read_text() == original_content, (
        "NOOP must not overwrite the file."
    )
    assert domain_file.stat().st_mtime == original_mtime, "NOOP must not alter mtime."


# ---------------------------------------------------------------------------
# Retry-loop behavior (parametrized — noop/failed/written)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invoke_return,expected_call_count",
    [
        (_DELTA_NOOP, 1),
        (None, MAX_DOMAIN_RETRIES),
        ("# Auth\n\nUpdated.", 1),
    ],
    ids=["noop-no-retry", "failed-exhausts-retries", "written-breaks-immediately"],
)
def test_update_affected_domains_retry_behavior(
    svc, config, versioned_dep_map_dirs, invoke_return, expected_call_count
):
    """
    _update_affected_domains invoke count varies by analyzer outcome:
    - NOOP (_DELTA_NOOP): exactly 1 — intentional no-op must not retry (Bug #849)
    - FAILED (None): exactly MAX_DOMAIN_RETRIES — failure exhausts retry budget
    - WRITTEN (content): exactly 1 — success breaks the loop immediately
    """
    _, dep_map_write_dir = versioned_dep_map_dirs
    svc._analyzer.invoke_delta_merge_file.return_value = invoke_return
    svc._analyzer.build_delta_merge_prompt.return_value = "merge prompt"

    svc._update_affected_domains(
        affected_domains={"auth"},
        dependency_map_dir=dep_map_write_dir,
        changed_repos=[{"alias": "repo1"}],
        new_repos=[],
        removed_repos=[],
        config=config,
    )

    actual = svc._analyzer.invoke_delta_merge_file.call_count
    assert actual == expected_call_count, (
        f"invoke_delta_merge_file called {actual} times; "
        f"expected {expected_call_count} for invoke_return={invoke_return!r}."
    )


# ---------------------------------------------------------------------------
# Bug #1069 regression guards — real analyzer, patch only the subprocess boundary
# ---------------------------------------------------------------------------

# Patch target: subprocess.run inside ClaudeInvoker (the external Claude dispatch boundary).
# The svc fixture creates DependencyMapService with codex=None so there is no failover;
# each domain-retry attempt makes exactly one subprocess.run call.
_CLAUDE_INVOKER_SUBPROCESS = (
    "code_indexer.server.services.claude_invoker.subprocess.run"
)


@pytest.fixture
def real_svc_env(tmp_path, config):
    """
    Real DependencyMapService with a real DependencyMapAnalyzer wired to a
    real CliDispatcher(ClaudeInvoker) so that subprocess.run is reachable.

    The analyzer's _cli_dispatcher is set directly so _get_cached_dispatcher
    returns it without calling get_config_service() (no server context needed).

    Returns (real_svc, dep_map_dir).
    """
    from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
    from code_indexer.server.services.cli_dispatcher import CliDispatcher
    from code_indexer.server.services.claude_invoker import ClaudeInvoker

    golden_repos_root = tmp_path / "golden-repos"
    golden_repos_root.mkdir()
    cidx_meta = tmp_path / "cidx-meta"
    cidx_meta.mkdir()

    claude_invoker = ClaudeInvoker(
        analysis_model="claude-sonnet-4-5",
        soft_timeout_seconds=10,
    )
    dispatcher = CliDispatcher(claude=claude_invoker, codex=None)

    analyzer = DependencyMapAnalyzer(
        golden_repos_root=golden_repos_root,
        cidx_meta_path=cidx_meta,
        pass_timeout=60,
        cli_dispatcher=dispatcher,
    )

    gm = Mock()
    gm.golden_repos_dir = str(tmp_path)
    tracking = Mock()
    tracking.get_tracking.return_value = {"status": "pending", "commit_hashes": None}
    real_svc = DependencyMapService(gm, Mock(), tracking, analyzer)
    real_svc._activity_journal = Mock()
    real_svc._activity_journal.log = Mock()

    dep_map_dir = tmp_path / "dep-map"
    dep_map_dir.mkdir()
    (dep_map_dir / "auth.md").write_text(
        "---\ndomain: auth\n---\n\n# Auth\n\nOriginal content."
    )
    return real_svc, dep_map_dir


def test_bug1069_mtime_unchanged_noop_does_not_retry(real_svc_env, config):
    """
    Bug #1069 money-pit regression guard (service level, real analyzer).

    When Claude runs successfully but does NOT edit the domain file (mtime
    unchanged), invoke_delta_merge_file must return _DELTA_NOOP.  The service
    retry loop must break immediately — subprocess.run invoked exactly ONCE,
    not MAX_DOMAIN_RETRIES times.

    Before the fix the mtime-unchanged path returned None (treated as FAILED),
    triggering up to MAX_DOMAIN_RETRIES Claude calls (~20 min each) for every
    unchanged domain.
    """
    real_svc, dep_map_dir = real_svc_env
    call_count = []

    def noop_subprocess(*args, **kwargs):
        call_count.append(1)
        # returncode=0, empty stdout → ClaudeInvoker reports success; no file edit.
        return _subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with _patch(_CLAUDE_INVOKER_SUBPROCESS, side_effect=noop_subprocess):
        real_svc._update_affected_domains(
            affected_domains={"auth"},
            dependency_map_dir=dep_map_dir,
            changed_repos=[{"alias": "repo1"}],
            new_repos=[],
            removed_repos=[],
            config=config,
        )

    assert len(call_count) == 1, (
        f"subprocess.run called {len(call_count)} times for a mtime-unchanged "
        f"domain; expected exactly 1 (bug #1069: no-op must NOT trigger retries). "
        f"Before the fix this was {MAX_DOMAIN_RETRIES}."
    )


def test_bug1069_dispatch_failure_retries_max_times(real_svc_env, config):
    """
    Bug #1069 companion guard — genuine dispatch failures must still exhaust retries.

    When Claude exits with a non-zero returncode, invoke_delta_merge_file returns
    None (retryable failure).  The svc fixture uses codex=None (no failover), so
    each domain-retry attempt makes exactly one subprocess.run call.
    Total calls must equal MAX_DOMAIN_RETRIES — proving the retry path still works.
    """
    real_svc, dep_map_dir = real_svc_env
    call_count = []

    def failing_subprocess(*args, **kwargs):
        call_count.append(1)
        return _subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error"
        )

    with _patch(_CLAUDE_INVOKER_SUBPROCESS, side_effect=failing_subprocess):
        real_svc._update_affected_domains(
            affected_domains={"auth"},
            dependency_map_dir=dep_map_dir,
            changed_repos=[{"alias": "repo1"}],
            new_repos=[],
            removed_repos=[],
            config=config,
        )

    assert len(call_count) == MAX_DOMAIN_RETRIES, (
        f"subprocess.run called {len(call_count)} times for a failing domain; "
        f"expected exactly {MAX_DOMAIN_RETRIES} (genuine failures must still retry). "
        f"The retry loop must not have been accidentally collapsed into a no-op."
    )
