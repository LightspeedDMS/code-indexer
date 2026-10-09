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


# ── incremental: what changed between two commits ──────────────────────────

OLD, NEW = "a" * 40, "b" * 40


class _DiffingGitMcp(_GitMcp):
    """``diff --name-only`` per direction, and the paths tracked at HEAD."""

    def __init__(self, forward: List[str], reverse: List[str], tracked: List[str]):
        super().__init__()
        self.forward, self.reverse, self.tracked = forward, reverse, tracked

    def diff_name_only(self, repo_path: str, spec: str, **kw: Any) -> List[str]:
        self._check()
        return self.forward if spec == f"{OLD}..{NEW}" else self.reverse

    def ls_files(self, repo_path: str, **kw: Any) -> List[str]:
        self._check()
        return self.tracked


def _indexer(repo: Path, git: Any) -> Any:
    from code_indexer.indexing.file_finder import FileFinder
    from code_indexer.services.smart_indexer import SmartIndexer

    config = Config(codebase_dir=repo, file_extensions=["py"])
    indexer = SmartIndexer.__new__(SmartIndexer)
    indexer.config = config
    indexer.git_backend = git
    indexer.file_finder = FileFinder(config)
    return indexer


def test_the_delta_deletes_a_renamed_files_old_path(empty_repo: Path) -> None:
    """With rename detection the forward diff names only the new path; the
    reverse diff names the old one, so its chunks are removed."""
    git = _DiffingGitMcp(
        forward=["src/edited.py", "src/new_name.py", "src/removed.py", "README.md"],
        reverse=[
            "src/edited.py",
            "src/old_name.py",
            "src/removed.py",
            "README.md",
            "docs/old.md",
        ],
        tracked=["src/edited.py", "src/new_name.py", "README.md", "src/untouched.py"],
    )

    delta = _indexer(empty_repo, git)._git_deltas_via_seam(OLD, NEW)

    assert delta is not None
    assert delta.modified == ["src/edited.py", "src/new_name.py"]
    assert delta.deleted == ["src/old_name.py", "src/removed.py"]
    assert "README.md" not in delta.modified, "the extension filter was skipped"
    assert "docs/old.md" not in delta.deleted, (
        "deletions skip the filter (local parity)"
    )


def test_a_failing_backend_is_not_read_as_nothing_changed(empty_repo: Path) -> None:
    git = _DiffingGitMcp([], [], [])
    git.down = True
    assert _indexer(empty_repo, git)._git_deltas_via_seam(OLD, NEW) is None


def test_an_incremental_run_without_a_delta_does_a_full_index(empty_repo: Path) -> None:
    """An empty delta would leave the index stale without a word."""
    git = _DiffingGitMcp([], [], [])
    git.down = True
    indexer = _indexer(empty_repo, git)
    metadata = MagicMock()
    metadata.can_resume_interrupted_operation.return_value = False
    metadata.get_resume_timestamp.return_value = 1_700_000_000.0
    metadata.get_last_indexed_commit.return_value = OLD
    indexer.progressive_metadata = metadata
    full: List[Any] = []
    indexer._do_full_index = lambda *a, **k: full.append(a) or "FULL"

    result = indexer._do_incremental_index(
        10,
        None,
        {"git_available": True, "current_commit": NEW, "current_branch": "main"},
        "voyage-ai",
        "voyage-code-3",
        60,
        quiet=True,
    )

    assert result == "FULL" and len(full) == 1


# ── review follow-ups (!143) ────────────────────────────────────────────────


class _BranchCallFails(_GitMcp):
    """git-mcp fails the symbolic-ref call only (not a detached HEAD)."""

    def symbolic_ref(self, repo_path: str, short: bool = True, **kw: Any) -> str:
        self.calls.append("symbolic_ref")
        raise subprocess.CalledProcessError(
            1, "git-mcp", stderr="git-mcp git_symbolic_ref request failed: reset"
        )


def test_a_failed_branch_lookup_is_unknown_not_detached(empty_repo: Path) -> None:
    """Reading it as detached-<sha> would report a branch change that did not
    happen, and detect_branch_change would force a re-index."""
    assert read_head_branch(empty_repo, _BranchCallFails()) == "unknown"


class _NotARepository(_GitMcp):
    def rev_parse(self, repo_path: str, *args: str, **kw: Any) -> str:
        self.calls.append("rev_parse " + " ".join(args))
        raise subprocess.CalledProcessError(
            128, "git-mcp", stderr="fatal: not a git repository"
        )


def test_a_non_git_source_stays_non_git_under_a_git_backend(empty_repo: Path) -> None:
    """The server stamps the backend on every spawn, cidx-meta and local://
    folders included. Flipping them to git-addressable would switch their file
    signatures to git hashes and change their point ids."""
    (empty_repo / "notes.md").write_text("hello\n")
    plain = FileIdentifier(empty_repo)
    behind_backend = FileIdentifier(empty_repo, git_backend=_NotARepository())

    assert behind_backend.git_available is plain.git_available is False
    assert behind_backend._get_cached_commit_hash() is None
    note = empty_repo / "notes.md"
    assert behind_backend.get_file_signature(
        behind_backend.get_file_metadata(note)
    ) == plain.get_file_signature(plain.get_file_metadata(note))


def test_the_delta_classifies_against_the_passs_manifest(empty_repo: Path) -> None:
    """The manifest is already in hand; ls_files grows with the repository."""

    class _NoLsFiles(_DiffingGitMcp):
        def ls_files(self, repo_path: str, **kw: Any) -> List[str]:
            raise AssertionError("ls_files called although the pass has a manifest")

    git = _NoLsFiles(
        forward=["src/edited.py", "src/new_name.py"],
        reverse=["src/edited.py", "src/old_name.py"],
        tracked=[],
    )
    indexer = _indexer(empty_repo, git)
    manifest = MagicMock()
    manifest.paths.return_value = ["src/edited.py", "src/new_name.py", "README.md"]
    indexer.repo_manifest = manifest

    delta = indexer._git_deltas_via_seam(OLD, NEW)

    assert delta is not None
    assert delta.modified == ["src/edited.py", "src/new_name.py"]
    assert delta.deleted == ["src/old_name.py"]


class _Blip(_GitMcp):
    """A real repository whose git-mcp fails transiently on rev-parse."""

    def rev_parse(self, repo_path: str, *args: str, **kw: Any) -> str:
        self.calls.append("rev_parse " + " ".join(args))
        raise subprocess.CalledProcessError(
            1, "git-mcp", stderr="git-mcp git_rev_parse request failed: timed out"
        )


def test_a_transient_failure_does_not_demote_a_real_repository(
    empty_repo: Path,
) -> None:
    """Demoting it would switch its signatures to file hashes (new point ids)
    and record git_available False, so the next healthy run re-indexes."""
    identifier = FileIdentifier(empty_repo, git_backend=_Blip())

    assert identifier.git_available is True
    assert identifier._get_cached_commit_hash() == "unknown"


def test_the_git_state_asks_the_backend_once(empty_repo: Path) -> None:
    from code_indexer.services.git_detection import GitDetectionService

    git = _GitMcp(branch="master")
    state = GitDetectionService(
        empty_repo, Config(codebase_dir=empty_repo), git_backend=git
    )._get_current_git_state()

    assert state["commit_hash"] == SHA and state["branch"] == "master"
    assert git.calls.count("rev_parse HEAD") == 1
