"""Unit tests for XrayPatternService coarse-lock coordination — Bug #1037.

Tests that store_xray_pattern() and delete_pattern() acquire the cidx-meta
coarse write lock (mirroring MemoryStoreService._run_with_coarse_lock) before
touching the shared git index.

Anti-mock discipline: real XrayPatternService instances, real YAML files in
tmpdir, fake scheduler stub (not MagicMock) that records calls.
Git operations are patched at the _git_commit method level (matching existing
test approach in test_xray_pattern_service.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from code_indexer.server.services.xray_pattern_service import XrayPatternService

# ---------------------------------------------------------------------------
# Shared test fixture data
# ---------------------------------------------------------------------------

MINIMAL_EVALUATOR = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"

MINIMAL_PATTERN_YAML = f"""\
name: test-pattern
description: "A test pattern"
language: java
evaluator_code: |
  {MINIMAL_EVALUATOR}
"""

_COARSE_ALIAS = "cidx-meta"


# ---------------------------------------------------------------------------
# Fake scheduler stub — records calls, does NOT use MagicMock
# ---------------------------------------------------------------------------


class FakeScheduler:
    """Minimal scheduler stub that records lock-coordination calls."""

    def __init__(
        self,
        is_locked: bool = False,
        acquire_returns: bool = True,
    ) -> None:
        self._is_locked = is_locked
        self._acquire_returns = acquire_returns
        # Call records
        self.is_write_locked_calls: list[tuple[str, ...]] = []
        self.acquire_write_lock_calls: list[tuple[str, str]] = []
        self.release_write_lock_calls: list[tuple[str, str]] = []

    def is_write_locked(self, alias: str) -> bool:
        self.is_write_locked_calls.append((alias,))
        return self._is_locked

    def acquire_write_lock(self, alias: str, owner: str) -> bool:
        self.acquire_write_lock_calls.append((alias, owner))
        return self._acquire_returns

    def release_write_lock(self, alias: str, owner: str) -> None:
        self.release_write_lock_calls.append((alias, owner))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cidx_meta(tmp_path: Path) -> Path:
    """Return a temporary cidx-meta directory."""
    meta = tmp_path / "cidx-meta"
    meta.mkdir()
    return meta


# ---------------------------------------------------------------------------
# Test 1: store_xray_pattern acquires coarse lock when unlocked
# ---------------------------------------------------------------------------


def test_store_pattern_acquires_coarse_lock_when_unlocked(
    cidx_meta: Path,
) -> None:
    """acquire_write_lock must be called with alias='cidx-meta' and matching owner."""
    scheduler = FakeScheduler(is_locked=False, acquire_returns=True)
    svc = XrayPatternService(cidx_meta_path=cidx_meta, refresh_scheduler=scheduler)

    with patch.object(svc, "_git_commit", return_value=None):
        svc.store_xray_pattern(
            scope="__any__",
            pattern_yaml=MINIMAL_PATTERN_YAML,
            overwrite=True,
        )

    assert len(scheduler.acquire_write_lock_calls) == 1, (
        "acquire_write_lock must be called exactly once"
    )
    alias, owner = scheduler.acquire_write_lock_calls[0]
    assert alias == _COARSE_ALIAS, f"alias must be '{_COARSE_ALIAS}', got {alias!r}"
    assert owner == "xray-pattern:store:test-pattern", (
        f"owner must be 'xray-pattern:store:test-pattern', got {owner!r}"
    )

    assert len(scheduler.release_write_lock_calls) == 1, (
        "release_write_lock must be called exactly once"
    )
    rel_alias, rel_owner = scheduler.release_write_lock_calls[0]
    assert rel_alias == _COARSE_ALIAS
    assert rel_owner == owner


# ---------------------------------------------------------------------------
# Test 2: store_xray_pattern piggybacks when lock is already held
# ---------------------------------------------------------------------------


def test_store_pattern_piggybacks_when_already_locked(
    cidx_meta: Path,
) -> None:
    """When is_write_locked=True, acquire NOT called, file IS written, _git_commit NOT called."""
    scheduler = FakeScheduler(is_locked=True, acquire_returns=True)
    svc = XrayPatternService(cidx_meta_path=cidx_meta, refresh_scheduler=scheduler)

    git_commit_called: list[Any] = []

    def _capture_git_commit(files: list, message: str) -> None:
        git_commit_called.append((files, message))

    with patch.object(svc, "_git_commit", side_effect=_capture_git_commit):
        result = svc.store_xray_pattern(
            scope="__any__",
            pattern_yaml=MINIMAL_PATTERN_YAML,
            overwrite=True,
        )

    assert result.get("success") is True, f"Expected success, got {result}"
    assert len(scheduler.acquire_write_lock_calls) == 0, (
        "acquire_write_lock must NOT be called when piggybacking"
    )
    assert len(scheduler.release_write_lock_calls) == 0, (
        "release_write_lock must NOT be called when piggybacking"
    )
    # YAML file must be written even on piggyback path
    expected_yaml = cidx_meta / "xray-patterns" / "__any__" / "test-pattern.yaml"
    assert expected_yaml.exists(), "YAML file must be written even when piggybacking"
    # _git_commit must NOT be called (lock-holder's refresh picks it up via git add -A)
    assert len(git_commit_called) == 0, (
        "_git_commit must NOT be called in piggyback path"
    )


# ---------------------------------------------------------------------------
# Test 3: store races into piggyback when acquire returns False
# ---------------------------------------------------------------------------


def test_store_pattern_falls_through_to_piggyback_when_acquire_loses_race(
    cidx_meta: Path,
) -> None:
    """is_write_locked=False but acquire returns False → piggyback: YAML written, no commit, no release."""
    scheduler = FakeScheduler(is_locked=False, acquire_returns=False)
    svc = XrayPatternService(cidx_meta_path=cidx_meta, refresh_scheduler=scheduler)

    git_commit_called: list[Any] = []

    def _capture_git_commit(files: list, message: str) -> None:
        git_commit_called.append((files, message))

    with patch.object(svc, "_git_commit", side_effect=_capture_git_commit):
        result = svc.store_xray_pattern(
            scope="__any__",
            pattern_yaml=MINIMAL_PATTERN_YAML,
            overwrite=True,
        )

    assert result.get("success") is True, f"Expected success, got {result}"
    expected_yaml = cidx_meta / "xray-patterns" / "__any__" / "test-pattern.yaml"
    assert expected_yaml.exists(), "YAML file must be written even in race-piggyback"
    assert len(git_commit_called) == 0, "_git_commit must NOT be called in piggyback"
    assert len(scheduler.release_write_lock_calls) == 0, (
        "release_write_lock must NOT be called (we never owned the lock)"
    )


# ---------------------------------------------------------------------------
# Test 4: coarse lock released when operation raises
# ---------------------------------------------------------------------------


def test_store_pattern_releases_lock_when_operation_raises(
    cidx_meta: Path,
) -> None:
    """When we own the lock and _git_commit raises, release_write_lock still fires."""
    scheduler = FakeScheduler(is_locked=False, acquire_returns=True)
    svc = XrayPatternService(cidx_meta_path=cidx_meta, refresh_scheduler=scheduler)

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("git boom")

    with patch.object(svc, "_git_commit", side_effect=_raise):
        with pytest.raises(RuntimeError, match="git boom"):
            svc.store_xray_pattern(
                scope="__any__",
                pattern_yaml=MINIMAL_PATTERN_YAML,
                overwrite=True,
            )

    assert len(scheduler.release_write_lock_calls) == 1, (
        "release_write_lock must be called exactly once even when operation raises"
    )
    rel_alias, rel_owner = scheduler.release_write_lock_calls[0]
    assert rel_alias == _COARSE_ALIAS
    assert rel_owner == "xray-pattern:store:test-pattern"


# ---------------------------------------------------------------------------
# Test 5: lock NOT released when piggybacking and operation raises
# ---------------------------------------------------------------------------


def test_store_pattern_does_not_release_when_piggybacking(
    cidx_meta: Path,
) -> None:
    """Piggyback path: if file write raises, release_write_lock is NOT called (never owned the lock)."""
    scheduler = FakeScheduler(is_locked=True, acquire_returns=True)
    svc = XrayPatternService(cidx_meta_path=cidx_meta, refresh_scheduler=scheduler)

    # Patch write_text on the specific path object to simulate disk full
    with patch(
        "code_indexer.server.services.xray_pattern_service.Path.write_text",
        side_effect=OSError("disk full"),
    ):
        with pytest.raises(OSError, match="disk full"):
            svc.store_xray_pattern(
                scope="__any__",
                pattern_yaml=MINIMAL_PATTERN_YAML,
                overwrite=True,
            )

    assert len(scheduler.release_write_lock_calls) == 0, (
        "release_write_lock must NOT be called when piggybacking (never owned the lock)"
    )


# ---------------------------------------------------------------------------
# Test 6: delete_pattern acquires coarse lock
# ---------------------------------------------------------------------------


def test_delete_pattern_acquires_coarse_lock(
    cidx_meta: Path,
) -> None:
    """delete_pattern must call acquire_write_lock with alias='cidx-meta' and owner='xray-pattern:delete:<name>'."""
    scheduler = FakeScheduler(is_locked=False, acquire_returns=True)
    svc = XrayPatternService(cidx_meta_path=cidx_meta, refresh_scheduler=scheduler)

    # Pre-create the pattern file so delete_pattern can find it
    scope_dir = cidx_meta / "xray-patterns" / "__any__"
    scope_dir.mkdir(parents=True)
    (scope_dir / "test-pattern.yaml").write_text(MINIMAL_PATTERN_YAML, encoding="utf-8")

    with patch.object(svc, "_git_commit", return_value=None):
        result = svc.delete_pattern(scope="__any__", name="test-pattern")

    assert result.get("success") is True, f"Expected success, got {result}"
    assert len(scheduler.acquire_write_lock_calls) == 1
    alias, owner = scheduler.acquire_write_lock_calls[0]
    assert alias == _COARSE_ALIAS
    assert owner == "xray-pattern:delete:test-pattern"
    assert len(scheduler.release_write_lock_calls) == 1
    rel_alias, rel_owner = scheduler.release_write_lock_calls[0]
    assert rel_alias == _COARSE_ALIAS
    assert rel_owner == owner


# ---------------------------------------------------------------------------
# Test 7: delete_pattern piggybacks when lock held — file still removed
# ---------------------------------------------------------------------------


def test_delete_pattern_piggybacks_when_locked(
    cidx_meta: Path,
) -> None:
    """When piggybacking, delete_pattern removes file from disk but skips _git_commit."""
    scheduler = FakeScheduler(is_locked=True, acquire_returns=True)
    svc = XrayPatternService(cidx_meta_path=cidx_meta, refresh_scheduler=scheduler)

    # Pre-create pattern file
    scope_dir = cidx_meta / "xray-patterns" / "__any__"
    scope_dir.mkdir(parents=True)
    pattern_file = scope_dir / "test-pattern.yaml"
    pattern_file.write_text(MINIMAL_PATTERN_YAML, encoding="utf-8")

    git_commit_called: list[Any] = []

    def _capture_git_commit(files: list, message: str) -> None:
        git_commit_called.append((files, message))

    with patch.object(svc, "_git_commit", side_effect=_capture_git_commit):
        result = svc.delete_pattern(scope="__any__", name="test-pattern")

    assert result.get("success") is True, f"Expected success, got {result}"
    assert not pattern_file.exists(), (
        "File must be removed from disk even when piggybacking"
    )
    assert len(git_commit_called) == 0, (
        "_git_commit must NOT be called when piggybacking"
    )
    assert len(scheduler.acquire_write_lock_calls) == 0
    assert len(scheduler.release_write_lock_calls) == 0


# ---------------------------------------------------------------------------
# Test 8: no scheduler injected — falls back to direct _git_commit
# ---------------------------------------------------------------------------


def test_service_falls_back_when_no_scheduler_injected(
    cidx_meta: Path,
) -> None:
    """XrayPatternService(no scheduler) must complete successfully and call _git_commit."""
    svc = XrayPatternService(cidx_meta_path=cidx_meta)  # no scheduler

    git_commit_called: list[Any] = []

    def _capture_git_commit(files: list, message: str) -> None:
        git_commit_called.append((files, message))

    with patch.object(svc, "_git_commit", side_effect=_capture_git_commit):
        result = svc.store_xray_pattern(
            scope="__any__",
            pattern_yaml=MINIMAL_PATTERN_YAML,
            overwrite=True,
        )

    assert result.get("success") is True, f"Expected success, got {result}"
    expected_yaml = cidx_meta / "xray-patterns" / "__any__" / "test-pattern.yaml"
    assert expected_yaml.exists(), "YAML must be written when no scheduler"
    assert len(git_commit_called) == 1, (
        "_git_commit must be called when no scheduler (pre-fix fallback behavior)"
    )


# ---------------------------------------------------------------------------
# Test 9: owner string format for debugging
# ---------------------------------------------------------------------------


def test_owner_string_format_for_debugging(
    cidx_meta: Path,
) -> None:
    """Owner string must follow 'xray-pattern:<verb>:<name>' for operator traceability."""
    scheduler = FakeScheduler(is_locked=False, acquire_returns=True)
    svc = XrayPatternService(cidx_meta_path=cidx_meta, refresh_scheduler=scheduler)

    with patch.object(svc, "_git_commit", return_value=None):
        svc.store_xray_pattern(
            scope="my-repo",
            pattern_yaml=MINIMAL_PATTERN_YAML,
            overwrite=True,
        )

    assert len(scheduler.acquire_write_lock_calls) == 1
    _, owner = scheduler.acquire_write_lock_calls[0]
    # Format: "xray-pattern:store:<name>"
    parts = owner.split(":")
    assert len(parts) == 3, f"Owner must have 3 colon-separated parts, got: {owner!r}"
    assert parts[0] == "xray-pattern", (
        f"First segment must be 'xray-pattern', got {parts[0]!r}"
    )
    assert parts[1] == "store", f"Second segment must be 'store', got {parts[1]!r}"
    assert parts[2] == "test-pattern", (
        f"Third segment must be pattern name, got {parts[2]!r}"
    )


# ---------------------------------------------------------------------------
# Test 10: lock alias is canonical "cidx-meta"
# ---------------------------------------------------------------------------


def test_lock_alias_is_canonical_cidx_meta(
    cidx_meta: Path,
) -> None:
    """All scheduler calls must use the alias 'cidx-meta' (same constant as MemoryStoreService)."""
    scheduler = FakeScheduler(is_locked=False, acquire_returns=True)
    svc = XrayPatternService(cidx_meta_path=cidx_meta, refresh_scheduler=scheduler)

    with patch.object(svc, "_git_commit", return_value=None):
        svc.store_xray_pattern(
            scope="__any__",
            pattern_yaml=MINIMAL_PATTERN_YAML,
            overwrite=True,
        )

    all_aliases = [c[0] for c in scheduler.is_write_locked_calls]
    all_aliases += [c[0] for c in scheduler.acquire_write_lock_calls]
    all_aliases += [c[0] for c in scheduler.release_write_lock_calls]

    for alias in all_aliases:
        assert alias == "cidx-meta", (
            f"All scheduler calls must use alias 'cidx-meta', got {alias!r}"
        )
