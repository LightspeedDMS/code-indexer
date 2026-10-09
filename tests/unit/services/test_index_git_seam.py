"""The index records HEAD's real commit and branch on a pod without the repo.

The ``cidx index`` child records the commit and branch it indexed: in the
index metadata (``current_commit``, which refresh compares with HEAD) and on
every chunk. Both used to come from git run here; on a pod that holds only
``.code-indexer`` they were "unknown", so refresh saw a stale index every cycle
and re-indexed from scratch -- measured on EKS. Pinned against that shape (an
empty directory) with a git backend that answers for the repository elsewhere:

* the shared HEAD reader asks the backend, and degrades to "unknown" -- not to
  a guess -- when the backend fails;
* without a backend the local commands are unchanged (real git);
* FileIdentifier (per chunk) and the processor's git status (the metadata) both
  carry the backend's answers;
* the child builds the backend the server's flag names.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.config import Config
from code_indexer.services.file_identifier import FileIdentifier
from code_indexer.services.git_head import read_head_branch, read_head_commit

SHA = "c0ffee" + "0" * 34
DETACHED = "fatal: ref HEAD is not a symbolic ref"


class _GitMcp:
    owns_credentials = True

    def __init__(
        self, *, branch: Optional[str] = "main", down: bool = False, dirty: str = ""
    ) -> None:
        self.branch = branch
        self.down = down
        self.dirty = dirty
        self.calls: List[str] = []

    def _check(self) -> None:
        if self.down:
            raise subprocess.CalledProcessError(
                1, "git-mcp", stderr="git-mcp request failed: connection refused"
            )

    def rev_parse(self, repo_path: str, *args: str, **kw: Any) -> str:
        self.calls.append("rev_parse " + " ".join(args))
        self._check()
        return SHA[:7] if "--short" in args else SHA

    def symbolic_ref(self, repo_path: str, short: bool = True, **kw: Any) -> str:
        self.calls.append("symbolic_ref")
        self._check()
        if self.branch is None:
            raise subprocess.CalledProcessError(128, "git-mcp", stderr=DETACHED)
        return self.branch

    def status_porcelain(self, repo_path: str, **kw: Any) -> str:
        self._check()
        return self.dirty


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    """What a mount-less pod holds at the golden repo's path."""
    repo = tmp_path / "golden-repos" / "agent-router"
    (repo / ".code-indexer").mkdir(parents=True)
    return repo


# ── the shared reader ───────────────────────────────────────────────────────


def test_head_comes_from_the_backend(empty_repo: Path) -> None:
    git = _GitMcp(branch="master")
    assert read_head_commit(empty_repo, git) == SHA
    assert read_head_branch(empty_repo, git) == "master"


def test_a_detached_head_is_named_by_its_short_hash(empty_repo: Path) -> None:
    assert read_head_branch(empty_repo, _GitMcp(branch=None)) == f"detached-{SHA[:7]}"


def test_an_unreachable_backend_degrades_to_unknown(empty_repo: Path) -> None:
    git = _GitMcp(down=True)
    assert read_head_commit(empty_repo, git) == "unknown"
    assert read_head_branch(empty_repo, git) == "unknown"


def test_without_a_backend_it_is_the_local_git_answer(tmp_path: Path) -> None:
    repo = tmp_path / "local"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "trunk")
    git(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )

    assert read_head_commit(repo, None) == git("rev-parse", "HEAD")
    assert read_head_branch(repo, None) == "trunk"
    git("checkout", "-q", "--detach")
    assert (
        read_head_branch(repo, None)
        == f"detached-{git('rev-parse', '--short', 'HEAD')}"
    )


# ── per chunk ───────────────────────────────────────────────────────────────


def test_every_chunk_carries_the_backends_commit_and_branch(empty_repo: Path) -> None:
    git = _GitMcp(branch="master")
    identifier = FileIdentifier(empty_repo, git_backend=git)

    assert identifier.git_available, "a backend makes the repository git-addressable"
    assert identifier._get_cached_commit_hash() == SHA
    assert identifier._get_cached_branch() == "master"
    identifier._get_cached_commit_hash()
    identifier._get_cached_branch()
    assert git.calls.count("rev_parse HEAD") == 1, "not memoized per run"
    assert git.calls.count("symbolic_ref") == 1, "not memoized per run"


def test_detaching_the_manifest_keeps_the_backend(empty_repo: Path) -> None:
    identifier = FileIdentifier(empty_repo, git_backend=_GitMcp())
    identifier.set_manifest(None)
    assert identifier.git_available


# ── the index metadata ──────────────────────────────────────────────────────


def test_the_index_metadata_records_the_backends_head(empty_repo: Path) -> None:
    """get_git_status() is what progressive metadata records as current_commit."""
    from code_indexer.services.git_aware_processor import GitAwareDocumentProcessor

    processor = GitAwareDocumentProcessor(
        Config(codebase_dir=empty_repo),
        MagicMock(),
        MagicMock(),
        git_backend=_GitMcp(branch="master", dirty=" M src/a.py\n"),
    )
    state = processor.git_detection._get_current_git_state()

    assert state["git_available"] is True
    assert state["commit_hash"] == SHA
    assert state["branch"] == "master"
    assert state["repo_root"] == str(empty_repo)
    assert state["has_uncommitted_changes"] is True
    # ...and the chunks this processor writes carry the same answer.
    assert processor.file_identifier._get_cached_commit_hash() == SHA


# ── the child builds the backend the flag names ─────────────────────────────


def test_the_child_builds_the_plugin_the_flag_selects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.cli import _build_index_git_backend

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "fake_git_mcp.py").write_text(
        "class _B:\n    owns_credentials = True\ndef make():\n    return _B()\n"
    )
    monkeypatch.syspath_prepend(str(plugin))
    monkeypatch.setenv("CIDX_GIT_BACKEND", "fake_git_mcp:make")

    backend = _build_index_git_backend("delegated")

    assert type(backend).__name__ == "_B"


def test_every_indexer_the_index_command_builds_gets_the_git_backend() -> None:
    """Source-level: the index command builds SmartIndexers for the primary and
    each extra provider. One without the backend would record "unknown" for
    its provider's metadata while the others were right."""
    import ast
    import inspect

    from code_indexer import cli

    tree = ast.parse(inspect.getsource(cli))
    index_fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "index"
    )
    calls = [
        n
        for n in ast.walk(index_fn)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "SmartIndexer"
    ]
    assert len(calls) >= 2, "the scan is probably broken"
    for call in calls:
        passed = {kw.arg: kw.value for kw in call.keywords}
        assert "git_backend" in passed, f"SmartIndexer at line {call.lineno}"
        assert getattr(passed["git_backend"], "id", None) == "repo_git_backend"
