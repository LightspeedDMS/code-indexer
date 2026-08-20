"""EVO-66445: unit tests for FTS backfill discovery + tick."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from code_indexer.server.services.fts_index_builder import (
    FTS_INDEX_DIRNAME,
    FtsBuildOutcome,
    FtsBuildStatus,
)
from code_indexer.server.services.fts_backfill.discovery import (
    FtsBackfillCandidate,
    discover_repos_missing_fts,
)
from code_indexer.server.services.fts_backfill.sweep import run_fts_backfill_tick


class _FakeGRM:
    """Minimal golden_repo_manager surface: list_golden_repos + get_actual_repo_path."""

    def __init__(self, entries: List[Dict[str, str]], paths: Dict[str, Path]) -> None:
        self._entries = entries
        self._paths = paths

    def list_golden_repos(self) -> List[Dict[str, str]]:
        return self._entries

    def get_actual_repo_path(self, alias: str) -> str:
        if alias not in self._paths:
            raise KeyError(f"no path for {alias}")
        return str(self._paths[alias])


def _repo(tmp_path: Path, name: str, *, fts: bool) -> Path:
    root = tmp_path / name
    ci = root / ".code-indexer"
    ci.mkdir(parents=True)
    if fts:
        d = ci / FTS_INDEX_DIRNAME
        d.mkdir()
        (d / "meta.json").write_text("{}")
    return root


# --------------------------- discovery ---------------------------


def test_discovery_returns_only_repos_missing_fts(tmp_path: Path) -> None:
    a = _repo(tmp_path, "a", fts=False)  # missing
    b = _repo(tmp_path, "b", fts=True)  # built
    c = _repo(tmp_path, "c", fts=False)  # missing
    grm = _FakeGRM(
        [{"alias": "a"}, {"alias": "b"}, {"alias": "c"}],
        {"a": a, "b": b, "c": c},
    )
    missing = discover_repos_missing_fts(grm)
    assert sorted(m.alias for m in missing) == ["a", "c"]


def test_discovery_accepts_alias_name_key_and_skips_bad_entries(tmp_path: Path) -> None:
    a = _repo(tmp_path, "a", fts=False)
    grm = _FakeGRM(
        [
            {"alias_name": "a"},  # legacy key
            {"nope": "x"},  # missing alias -> skipped
            {"alias": "gone"},  # unresolvable path -> skipped
        ],
        {"a": a},
    )
    missing = discover_repos_missing_fts(grm)
    assert [m.alias for m in missing] == ["a"]


# --------------------------- tick ---------------------------


def _ensure_stub(results):
    """Return an ensure_fn that pops a status per call keyed by alias."""

    def _ensure(repo_path, *, alias, subprocess_env=None, timeout=None):
        return FtsBuildOutcome(results[alias], alias, "stub")

    return _ensure


def test_tick_builds_up_to_batch_size(tmp_path: Path) -> None:
    cands = [
        FtsBackfillCandidate(alias=x, repo_path=tmp_path / x)
        for x in ("a", "b", "c", "d")
    ]
    results = {x: FtsBuildStatus.REBUILT for x in ("a", "b", "c", "d")}
    stats = run_fts_backfill_tick(
        object(),
        batch_size=2,
        ensure_fn=_ensure_stub(results),
        discover_fn=lambda _grm: cands,
    )
    assert stats.total_missing == 4
    assert stats.attempted == 2  # capped by batch_size
    assert stats.rebuilt == 2
    assert stats.remaining_after_tick == 2


def test_tick_counts_failures_loudly_without_aborting(tmp_path: Path) -> None:
    cands = [
        FtsBackfillCandidate(alias=x, repo_path=tmp_path / x) for x in ("ok", "bad")
    ]
    results = {"ok": FtsBuildStatus.REBUILT, "bad": FtsBuildStatus.FAILED}
    stats = run_fts_backfill_tick(
        object(),
        batch_size=10,
        ensure_fn=_ensure_stub(results),
        discover_fn=lambda _grm: cands,
    )
    assert stats.attempted == 2
    assert stats.rebuilt == 1
    assert stats.failed == 1
    assert stats.failed_aliases == ["bad"]


def test_tick_empty_fleet_is_complete_noop() -> None:
    stats = run_fts_backfill_tick(
        object(),
        batch_size=10,
        ensure_fn=_ensure_stub({}),
        discover_fn=lambda _grm: [],
    )
    assert stats.complete
    assert stats.attempted == 0
    assert stats.total_missing == 0
