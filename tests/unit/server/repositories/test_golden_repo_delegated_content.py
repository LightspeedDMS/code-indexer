"""Golden-repo registration when this pod does not hold the repositories.

Under ``content_backend='delegated'`` git-mcp clones onto ITS volume and every
read goes through workspace-mcp; this pod keeps only the repository's index
state. Pinned here, against a real ``git clone`` onto a separate "remote"
volume and a content backend that reads ONLY that volume:

* the clone goes through the git seam, and the local ``clone_path`` is created
  as an index-state directory holding none of the repository;
* registration waits until the clone is visible THROUGH the content backend,
  and fails with the backend's own error when it never is;
* the configurations that would silently index nothing are refused: a local git
  backend (the clone would land here, unread), and a local source that would
  have to be copied off this pod's disk;
* the server actually hands the configured backend to the manager.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GitOperationError,
    GoldenRepoManager,
)
from code_indexer.server.storage.shared.content_backend import LocalContentBackend
from code_indexer.server.storage.shared.git_backend import LocalGitBackend


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "app.py").write_text("print('hi')\n")
    _git("add", ".", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=repo)
    return repo


class _Volume:
    """The file service's volume: this pod's paths, rooted somewhere else."""

    def __init__(self, root: Path, pod_data: Path) -> None:
        self.root = root
        self.pod_data = pod_data

    def __call__(self, pod_path: Any) -> Path:
        return self.root / Path(pod_path).relative_to(self.pod_data)


REMOTE_URL = "https://git.example/upstream.git"


class _GitMcp:
    """A delegated git backend: clones land on the service's volume."""

    def __init__(self, volume: _Volume, upstream: Path) -> None:
        self.volume = volume
        self.upstream = upstream
        self.cloned: List[str] = []

    @property
    def owns_credentials(self) -> bool:
        return True

    def clone(self, repo_url: str, dest: Any, **kw: Any) -> None:
        assert repo_url == REMOTE_URL
        self.cloned.append(str(dest))
        target = self.volume(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "-q", str(self.upstream), str(target), cwd=target.parent)


class _WorkspaceMcp:
    """A delegated content backend reading only the service's volume."""

    is_local = False

    def __init__(self, volume: _Volume, *, unseen_for: int = 0) -> None:
        self.volume = volume
        self.unseen_for = unseen_for
        self.calls = 0

    def manifest(self, repo_path: Any, **kw: Any) -> Any:
        self.calls += 1
        target = self.volume(repo_path)
        if self.calls <= self.unseen_for or not target.is_dir():
            raise RuntimeError("manifest returned 404: no such repository")
        return LocalContentBackend().manifest(target, **kw)

    def fetch(self, repo_path: Any, paths: Any) -> Any:
        return LocalContentBackend().fetch(self.volume(repo_path), paths)


@pytest.fixture
def pod(tmp_path: Path, upstream: Path) -> Any:
    data = tmp_path / "pod-data"
    data.mkdir()
    volume = _Volume(tmp_path / "service-volume", data)
    git = _GitMcp(volume, upstream)

    def manager(**overrides: Any) -> GoldenRepoManager:
        backends: Dict[str, Any] = {
            "git_backend": git,
            "content_backend": _WorkspaceMcp(volume),
        }
        backends.update(overrides)
        return GoldenRepoManager(str(data), **backends)

    return manager, volume, git


def _fast(manager: GoldenRepoManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the visibility wait so a never-visible clone fails in milliseconds."""
    wait = manager._await_clone_visible_through_content_backend
    monkeypatch.setattr(
        manager,
        "_await_clone_visible_through_content_backend",
        lambda p: wait(p, attempts=3, delay_seconds=0.001),
    )


def test_a_remote_clone_lands_with_the_service_and_only_index_state_here(
    pod: Any,
) -> None:
    make, volume, git = pod
    manager = make()

    clone_path = manager._clone_repository(REMOTE_URL, "r", None)

    assert git.cloned == [clone_path], "the clone did not go through the git seam"
    assert (volume(clone_path) / "app.py").is_file(), "not on the service's volume"
    assert Path(clone_path).is_dir(), "no local index-state directory"
    assert list(Path(clone_path).iterdir()) == [], "repository files landed here"


def test_registration_waits_until_the_reader_sees_the_clone(
    pod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make, volume, _ = pod
    content = _WorkspaceMcp(volume, unseen_for=2)
    manager = make(content_backend=content)
    _fast(manager, monkeypatch)

    manager._clone_repository(REMOTE_URL, "r", None)

    assert content.calls == 3, "returned before the reader could list the clone"


def test_a_clone_the_reader_never_sees_fails_with_the_readers_error(
    pod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    make, volume, _ = pod
    content = _WorkspaceMcp(volume, unseen_for=10**6)
    manager = make(content_backend=content)
    _fast(manager, monkeypatch)

    with pytest.raises(GitOperationError, match="404: no such repository"):
        manager._clone_repository(REMOTE_URL, "r", None)
    assert not Path(manager.golden_repos_dir, "r").exists(), (
        "an index-state directory was created for a clone nobody can read"
    )


@pytest.mark.parametrize("in_place", [False, True])
def test_a_local_git_backend_is_refused_for_any_source(
    pod: Any, in_place: bool
) -> None:
    """In place too: ls-remote and the branch lookup would read this pod's disk."""
    make, _, git = pod
    manager = make(git_backend=LocalGitBackend())
    url = str(Path(manager.golden_repos_dir, "r")) if in_place else REMOTE_URL

    with pytest.raises(GitOperationError, match="git_backend='delegated'"):
        manager._clone_repository(url, "r", None)
    assert git.cloned == []
    assert not Path(manager.golden_repos_dir, "r").exists()


def test_submission_refuses_a_local_git_backend_before_the_pre_flight(
    pod: Any,
) -> None:
    """Otherwise ls-remote on a path this pod lacks reports an inaccessible repo."""
    make, _, _ = pod
    manager = make(git_backend=LocalGitBackend())

    with pytest.raises(GitOperationError, match="git_backend='delegated'"):
        manager.add_golden_repo(
            repo_url=str(Path(manager.golden_repos_dir, "r")),
            alias="r",
            submitter_username="admin",
        )


def test_a_local_source_that_would_be_copied_is_refused(
    pod: Any, upstream: Path
) -> None:
    make, _, git = pod
    manager = make()

    for url in (str(upstream), upstream.as_uri()):
        with pytest.raises(GitOperationError, match="in place"):
            manager._clone_repository(url, "r", None)
    assert git.cloned == []
    assert not Path(manager.golden_repos_dir, "r").exists()


def test_an_in_place_source_is_registered_without_a_clone(pod: Any) -> None:
    """Provisioned by workspace-mcp at the alias's path: index it where it is."""
    make, volume, git = pod
    manager = make()
    in_place = Path(manager.golden_repos_dir, "r")
    volume(in_place).mkdir(parents=True)
    (volume(in_place) / "a.py").write_text("x = 1\n")

    assert manager._clone_repository(str(in_place), "r", None) == str(in_place)
    assert git.cloned == []
    assert in_place.is_dir() and list(in_place.iterdir()) == []


def test_the_server_hands_the_configured_backend_to_the_manager() -> None:
    """Without it the manager defaults to local and copies or clones onto the pod."""
    from code_indexer.server.startup import service_init

    tree = ast.parse(inspect.getsource(service_init))
    bound = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "build_configured_content_backend"
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    passed = [
        kw.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "GoldenRepoManager"
        for kw in node.keywords
        if kw.arg == "content_backend" and isinstance(kw.value, ast.Name)
    ]
    assert passed and set(passed) <= bound, (passed, bound)


def test_temporal_with_local_git_is_refused_at_registration(pod: Any) -> None:
    """With git run on this pod, which holds no repository, there is no history."""
    make, _, git = pod
    manager = make(git_backend=LocalGitBackend())

    with pytest.raises(GitOperationError, match="git_backend='delegated'"):
        manager.add_golden_repo(
            repo_url=REMOTE_URL,
            alias="r",
            enable_temporal=True,
            submitter_username="admin",
        )
    assert git.cloned == []


def test_a_temporal_index_with_local_git_cannot_be_added_later(pod: Any) -> None:
    make, _, _ = pod
    manager = make(git_backend=LocalGitBackend())
    manager._resolve_golden_repo = lambda alias: object()  # registered

    with pytest.raises(GitOperationError, match="temporal indexing"):
        manager._submit_add_indexes_job(
            alias="r", index_types=["semantic", "temporal"], submitter_username="admin"
        )


def test_temporal_through_the_git_seam_is_accepted(pod: Any) -> None:
    """With git delegated the history is read through git-mcp: not refused."""
    make, _, _ = pod
    manager = make()

    manager._refuse_temporal_under_delegated_content()
