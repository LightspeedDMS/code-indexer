"""No trigram pre-filter where the repository is not on this disk.

The trigram index is built by listing and reading every file on THIS disk. On
a pod that holds only the repository's index state (content_backend
'delegated') that listing is empty. An empty index reads as absent (exists()
wants a populated one), so searches full-scan -- but every refresh and every
lazy self-heal (each 5 minutes) rebuilds it, empty, for nothing. And an index
built while the files WERE here (before the pod went mount-less) would still
be consulted, steering every search at paths this pod cannot read. Pinned:

* the rule: available only when the content backend is local, and not when the
  setting cannot be read (a missing pre-filter costs time, a wrong one results);
* refresh does not build it, and removes one an earlier build left;
* the lazy self-heal does not build it;
* regex search does not consult a populated one.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import List

import pytest

from code_indexer.global_repos import regex_search as rs
from code_indexer.global_repos import trigram_index_manager as tim
from code_indexer.server.startup import content_backend_wiring as wiring


@pytest.fixture
def delegated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wiring, "configured_content_backend_name", lambda: "delegated")


@pytest.fixture
def index_state_only(tmp_path: Path) -> Path:
    """The repo dir on a mount-less pod, holding an index built while the
    files were still here -- populated, so it would be consulted."""
    repo = tmp_path / "golden-repos" / "agent-router"
    (repo / ".code-indexer").mkdir(parents=True)
    (repo / "router.py").write_text("def route_request():\n    pass\n")
    assert tim.TrigramIndexManager(tim.trigram_index_dir(repo)).build(repo) == 1
    (repo / "router.py").unlink()  # the files are with workspace-mcp now
    return repo


@pytest.fixture(autouse=True)
def _reset_lazy_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CIDX_TRIGRAM_LAZY_BUILD", raising=False)
    rs._lazy_build_in_progress.clear()
    rs._lazy_build_last_attempt.clear()


def test_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wiring, "configured_content_backend_name", lambda: "local")
    assert tim.trigram_prefilter_available() is True
    monkeypatch.setattr(wiring, "configured_content_backend_name", lambda: "delegated")
    assert tim.trigram_prefilter_available() is False

    def unreadable() -> str:
        raise RuntimeError("config service not initialised")

    monkeypatch.setattr(wiring, "configured_content_backend_name", unreadable)
    assert tim.trigram_prefilter_available() is False


def test_refresh_removes_the_empty_index_and_builds_none(
    delegated: None, index_state_only: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.global_repos.refresh_scheduler import _build_source_trigram_index

    built: List[Path] = []
    monkeypatch.setattr(
        tim.TrigramIndexManager,
        "build",
        lambda self, p, file_list=None: built.append(p) or 0,
    )

    _build_source_trigram_index("agent-router-global", str(index_state_only))

    assert built == []
    assert not tim.trigram_index_dir(index_state_only).exists()


def test_the_lazy_self_heal_builds_nothing(
    delegated: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: List[Path] = []
    done = threading.Event()

    def fake_build(self: object, repo_path: Path, file_list: object = None) -> int:
        calls.append(repo_path)
        done.set()
        return 0

    monkeypatch.setattr(tim.TrigramIndexManager, "build", fake_build)
    rs._maybe_trigger_lazy_index_build(tmp_path)
    assert not done.wait(0.5)
    assert calls == []


def test_regex_search_does_not_consult_a_populated_index(
    delegated: None, index_state_only: Path
) -> None:
    """Consulting it would hand back router.py, which is not on this pod. Not
    consulting it means a full scan (None)."""
    assert tim.TrigramIndexManager(tim.trigram_index_dir(index_state_only)).exists()
    svc = rs.RegexSearchService(index_state_only)

    candidates = svc._prefilter_candidate_files(
        "route_request", index_state_only, None, True, deadline=time.monotonic() + 30
    )

    assert candidates is None
