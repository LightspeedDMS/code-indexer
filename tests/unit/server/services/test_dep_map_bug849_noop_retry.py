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

import pytest
from unittest.mock import Mock

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
