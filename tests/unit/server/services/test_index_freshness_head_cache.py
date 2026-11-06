"""index_freshness reuses a golden repo's HEAD from git-mcp per alias version.

Under ``content_backend: delegated`` every freshness check asked git-mcp
``rev_parse HEAD``: the FTS and SCIP backfill sweeps walk ~900 golden repos
every 5 minutes, so ~900 git-mcp calls per sweep per tick. HEAD only moves when
a refresh pulls, and a refresh that publishes moves the alias pointer -- so the
answer is cached per repository, keyed on the ``<alias>-global`` alias
document, with a TTL. Failures are never cached; locally nothing is cached.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List

import pytest

import code_indexer.server.startup.content_backend_wiring as content_wiring
import code_indexer.server.startup.git_backend_wiring as git_wiring
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.services import index_freshness
from code_indexer.server.services.index_freshness import (
    BUILD_COMMIT_MARKER,
    build_commit_is_current,
    current_commit,
    record_build_commit,
)

SHA_1 = "1" * 40
SHA_2 = "2" * 40
ALIAS = "example-repo"


class _GitMcp:
    def __init__(self, head: Any = SHA_1) -> None:
        self.head = head
        self.calls: List[str] = []

    def rev_parse(self, repo_path: str, *args: str, **kw: Any) -> str:
        self.calls.append(repo_path)
        if isinstance(self.head, BaseException):
            raise self.head
        return str(self.head)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    index_freshness._reset_head_cache()
    yield
    index_freshness._reset_head_cache()


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    """A mount-less pod's golden-repos dir: index state and a files-only alias."""
    root = tmp_path / "golden-repos"
    (root / ALIAS / ".code-indexer").mkdir(parents=True)
    AliasManager(root / "aliases").create_alias(
        f"{ALIAS}-global", str(root / ".versioned" / ALIAS / "v_1"), repo_name=ALIAS
    )
    return root


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch) -> _GitMcp:
    seam = _GitMcp()
    monkeypatch.setattr(content_wiring, "is_content_seam_delegated", lambda: True)
    monkeypatch.setattr(git_wiring, "server_delegated_git_backend", lambda: seam)
    return seam


def test_repeat_ticks_ask_git_mcp_once(golden: Path, git: _GitMcp) -> None:
    repo = golden / ALIAS
    for _ in range(5):
        assert current_commit(repo) == SHA_1
    assert len(git.calls) == 1


def test_an_alias_swap_invalidates_at_once(golden: Path, git: _GitMcp) -> None:
    repo = golden / ALIAS
    assert current_commit(repo) == SHA_1
    git.head = SHA_2
    AliasManager(golden / "aliases").swap_alias(
        f"{ALIAS}-global",
        str(golden / ".versioned" / ALIAS / "v_2"),
        str(golden / ".versioned" / ALIAS / "v_1"),
    )
    assert current_commit(repo) == SHA_2
    assert len(git.calls) == 2


def test_the_ttl_expires_an_entry(
    golden: Path, git: _GitMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = golden / ALIAS
    now = [1000.0]
    monkeypatch.setattr(index_freshness.time, "monotonic", lambda: now[0])
    current_commit(repo)
    now[0] += index_freshness.HEAD_CACHE_TTL_SECONDS - 1
    current_commit(repo)
    assert len(git.calls) == 1
    now[0] += 2
    current_commit(repo)
    assert len(git.calls) == 2


def test_a_failure_is_not_cached(golden: Path, git: _GitMcp) -> None:
    repo = golden / ALIAS
    git.head = ConnectionError("git-mcp unreachable")
    assert current_commit(repo) is None
    git.head = SHA_1
    assert current_commit(repo) == SHA_1
    assert len(git.calls) == 2


def test_a_failure_evicts_a_cached_value(golden: Path, git: _GitMcp) -> None:
    repo = golden / ALIAS
    assert current_commit(repo) == SHA_1
    git.head = ConnectionError("git-mcp unreachable")
    assert current_commit(repo, fresh=True) is None
    git.head = SHA_2
    assert current_commit(repo) == SHA_2


def test_not_a_repository_is_git_s_answer_and_cached(
    golden: Path, git: _GitMcp
) -> None:
    repo = golden / ALIAS
    git.head = subprocess.CalledProcessError(
        128, "git-mcp", stderr="fatal: not a git repository"
    )
    assert current_commit(repo) is None
    assert current_commit(repo) is None
    assert len(git.calls) == 1


def test_no_alias_means_no_cache(tmp_path: Path, git: _GitMcp) -> None:
    repo = tmp_path / "golden-repos" / "unaliased"
    repo.mkdir(parents=True)
    current_commit(repo)
    current_commit(repo)
    assert len(git.calls) == 2


def test_snapshot_path_uses_its_alias(golden: Path, git: _GitMcp) -> None:
    snapshot = golden / ".versioned" / ALIAS / "v_1"
    snapshot.mkdir(parents=True)
    assert index_freshness._alias_version(snapshot) == (
        index_freshness._alias_version(golden / ALIAS)
    )
    assert index_freshness._alias_version(snapshot) is not None


def test_record_build_commit_reads_fresh_and_refreshes_the_cache(
    golden: Path, git: _GitMcp
) -> None:
    repo = golden / ALIAS
    index_dir = repo / ".code-indexer" / "tantivy_index"
    index_dir.mkdir()
    assert current_commit(repo) == SHA_1
    git.head = SHA_2  # moved without an alias swap (out-of-band)
    record_build_commit(index_dir, repo)
    assert (index_dir / BUILD_COMMIT_MARKER).read_text() == SHA_2
    assert build_commit_is_current(repo, index_dir)
    assert len(git.calls) == 2


def test_local_reads_are_never_cached(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/master\n")
    ref = git_dir / "refs" / "heads" / "master"
    ref.write_text(SHA_1 + "\n")
    assert current_commit(repo) == SHA_1
    ref.write_text(SHA_2 + "\n")
    assert current_commit(repo) == SHA_2
    assert index_freshness._head_cache == {}
