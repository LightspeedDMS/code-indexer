"""SCIP's repository reads on a pod without the repository.

The pod holds only cidx's index state (``<repo>/.code-indexer``); the
repository itself lives where the content backend (workspace-mcp) and the git
backend (git-mcp) can see it. Discovery, the root build-file shape check and
HEAD must come from those seams -- and every local repository read is
forbidden here (``no_local_repo_reads``) so a regression cannot pass by quietly
reading a disk that, on EKS, is empty.

Parity: the content-seam answers must equal what the local code answers over
the same tree, and with a local content seam the local code must be what runs.
"""

from __future__ import annotations

import builtins
import os
import pathlib
import subprocess
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence, Tuple

import pytest

from code_indexer.scip import discovery as disc
from code_indexer.scip.discovery import (
    LocalBuildFileSource,
    ProjectDiscovery,
    has_root_build_file,
)
from code_indexer.server.services import scip_repo_reads
from code_indexer.server.services.scip_lifecycle import (
    ScipDelegationOutcome,
    funnel_scip_generation,
    record_scip_build_commit,
    scip_db_is_fresh,
)
from code_indexer.server.services.scip_repo_reads import (
    ContentBuildFileSource,
    ScipRepoReadError,
    build_file_source,
    head_commit,
    repo_has_root_build_file,
)
from code_indexer.server.services.scip_resolution_queue import (
    get_scip_metrics,
    reset_scip_metrics,
)
from code_indexer.server.startup import content_backend_wiring, git_backend_wiring
from code_indexer.server.storage.shared.content_backend import (
    ContentPage,
    FetchedFile,
    LocalContentBackend,
)

COMMIT = "a" * 40

# ── fixtures: a repository elsewhere, a pod with only index state ────────────


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


AGGREGATOR_POM = (
    '<project xmlns="http://maven.apache.org/POM/4.0.0">'
    "<modules><module>core</module></modules></project>"
)

MIXED_TREE = {
    "pom.xml": "<project/>",
    "jvm/settings.gradle": "include 'app'",
    "jvm/app/build.gradle": "",
    "lib/build.gradle.kts": "",
    "services/pom.xml": AGGREGATOR_POM,
    "services/core/pom.xml": "<project/>",
    "web/package.json": "{}",
    "web/node_modules/dep/package.json": "{}",
    "tools/go.mod": "module x",
    "py/pyproject.toml": "",
    "py/requirements.txt": "",
    "dotnet/App.sln": "",
    "dotnet/App.csproj": "",
    ".git/pom.xml": "<project/>",
    ".code-indexer/package.json": "{}",
    "docs/readme.md": "",
}


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """The repository as workspace-mcp sees it."""
    root = tmp_path / "remote" / "svc"
    for rel, text in MIXED_TREE.items():
        _write(root, rel, text)
    return root


@pytest.fixture
def pod(tmp_path: Path) -> Path:
    """The same repository path on the pod: index state only."""
    root = tmp_path / "pod" / "golden-repos" / "svc"
    (root / ".code-indexer" / "scip").mkdir(parents=True)
    return root


class _SeamBackend:
    """A delegated content backend: serves ``remote`` when asked about ``pod``."""

    is_local = False

    def __init__(self, pod: Path, remote: Path, *, prune: bool = True) -> None:
        self._pod = pod
        self._remote = remote
        self._local = LocalContentBackend()
        self.manifest_calls: List[Tuple[Optional[str], Any]] = []
        self.fetch_calls: List[List[str]] = []
        self.fail: Optional[Exception] = None
        if not prune:
            self.manifest = self._manifest_without_prune  # type: ignore[method-assign]

    def _where(self, repo_path: Any) -> Path:
        assert Path(repo_path) == self._pod, repo_path
        return self._remote

    def manifest(
        self,
        repo_path: Any,
        *,
        cursor: Optional[str] = None,
        page_size: int = 3,
        prune_dirs: Optional[Sequence[str]] = None,
    ) -> ContentPage:
        if self.fail is not None:
            raise self.fail
        self.manifest_calls.append((cursor, prune_dirs))
        return self._local.manifest(
            self._where(repo_path), cursor=cursor, page_size=3, prune_dirs=prune_dirs
        )

    def _manifest_without_prune(
        self, repo_path: Any, *, cursor: Optional[str] = None
    ) -> ContentPage:
        self.manifest_calls.append((cursor, None))
        return self._local.manifest(self._where(repo_path), cursor=cursor, page_size=3)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        if self.fail is not None:
            raise self.fail
        self.fetch_calls.append(list(paths))
        return self._local.fetch(self._where(repo_path), paths)


class _GitMcp:
    def __init__(self, commit: str = COMMIT) -> None:
        self.commit = commit
        self.calls: List[Tuple[str, Tuple[str, ...]]] = []
        self.fail: Optional[Exception] = None

    def rev_parse(self, repo_path: str, *args: str, timeout: Any = None) -> str:
        self.calls.append((repo_path, args))
        if self.fail is not None:
            raise self.fail
        return self.commit + "\n"


@pytest.fixture
def seams(
    monkeypatch: pytest.MonkeyPatch, pod: Path, remote: Path
) -> Tuple[_SeamBackend, _GitMcp]:
    backend = _SeamBackend(pod, remote)
    git = _GitMcp()
    monkeypatch.setattr(
        content_backend_wiring, "server_content_backend", lambda: backend
    )
    monkeypatch.setattr(git_backend_wiring, "server_delegated_git_backend", lambda: git)
    scip_repo_reads.reset_cache()
    yield backend, git
    scip_repo_reads.reset_cache()


@pytest.fixture
def no_local_repo_reads(monkeypatch: pytest.MonkeyPatch, pod: Path) -> None:
    """Any read of the pod's repository path outside its index state fails the test."""
    index_state = pod / ".code-indexer"

    def guard(path: Any) -> None:
        try:
            p = Path(os.fspath(path)).absolute()
        except TypeError:
            return
        if (p == pod or pod in p.parents) and not (
            p == index_state or index_state in p.parents
        ):
            raise AssertionError(f"local repository read on a mount-less pod: {p}")

    def wrap(owner: Any, name: str) -> None:
        original = getattr(owner, name)

        def checked(self: Any, *a: Any, **kw: Any) -> Any:
            guard(self)
            return original(self, *a, **kw)

        monkeypatch.setattr(owner, name, checked)

    for name in (
        "exists",
        "is_file",
        "is_dir",
        "read_text",
        "read_bytes",
        "glob",
        "rglob",
        "iterdir",
        "stat",
    ):
        wrap(pathlib.Path, name)

    real_walk, real_scandir, real_open = os.walk, os.scandir, builtins.open

    def walk(top: Any, *a: Any, **kw: Any) -> Any:
        guard(top)
        return real_walk(top, *a, **kw)

    def scandir(path: Any = ".") -> Any:
        guard(path)
        return real_scandir(path)

    def open_(file: Any, *a: Any, **kw: Any) -> Any:
        if isinstance(file, (str, os.PathLike)):
            guard(file)
        return real_open(file, *a, **kw)

    def no_subprocess(*a: Any, **kw: Any) -> Any:
        raise AssertionError(f"subprocess on a mount-less pod: {a} {kw.get('cwd')}")

    monkeypatch.setattr(os, "walk", walk)
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(builtins, "open", open_)
    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)


def _key(projects: Sequence[Any]) -> List[Tuple[str, str, str, str]]:
    return sorted(
        (str(p.relative_path), p.language, p.build_system, str(p.build_file))
        for p in projects
    )


# ── parity: content seam == local, over the same tree ─────────────────────────


def test_discovery_parity(seams: Any, pod: Path, remote: Path) -> None:
    local = ProjectDiscovery(remote).discover()
    backend, _ = seams
    delegated = ProjectDiscovery(
        pod, ContentBuildFileSource(backend, pod, pod)
    ).discover()
    assert _key(local) == _key(delegated)
    assert len(local) >= 7  # the fixture really exercises gradle/maven/npm/go/py/c#


@pytest.mark.parametrize(
    "files",
    [
        {"pom.xml": "<project/>"},
        {"settings.gradle.kts": ""},
        {"App.sln": ""},
        {"sub/App.sln": "", "sub/pom.xml": ""},
        {"README.md": ""},
        {},
    ],
)
def test_root_build_file_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict
) -> None:
    remote = tmp_path / "r" / "svc"
    pod = tmp_path / "p" / "svc"
    remote.mkdir(parents=True)
    pod.mkdir(parents=True)
    for rel, text in files.items():
        _write(remote, rel, text)
    backend = _SeamBackend(pod, remote)
    expected = has_root_build_file(remote)
    assert (
        has_root_build_file(pod, ContentBuildFileSource(backend, pod, pod)) is expected
    )


def test_local_seam_runs_the_local_code_unchanged(
    monkeypatch: pytest.MonkeyPatch, remote: Path
) -> None:
    """With a local content seam nothing is delegated: same functions, same answers."""
    monkeypatch.setattr(
        content_backend_wiring, "server_content_backend", lambda: LocalContentBackend()
    )
    assert build_file_source(remote) is None
    assert head_commit(remote, local_reader=lambda p: f"local:{p.name}") == "local:svc"
    assert repo_has_root_build_file(remote) is has_root_build_file(remote) is True
    monkeypatch.setattr(content_backend_wiring, "server_content_backend", lambda: None)
    assert build_file_source(remote) is None


def test_default_discovery_walks_exactly_as_os_walk(remote: Path) -> None:
    """LocalBuildFileSource is the pre-seam walk: same files, same order, pruned."""
    expected = []
    for dirpath, dirnames, filenames in os.walk(remote):
        dirnames[:] = [d for d in dirnames if d not in disc.DISCOVERY_PRUNE_DIRS]
        expected.extend(Path(dirpath) / f for f in filenames)
    assert list(LocalBuildFileSource(remote).iter_files()) == expected
    assert isinstance(ProjectDiscovery(remote)._source, LocalBuildFileSource)


# ── no local reads on a mount-less pod ──────────────────────────────────────


def test_discovery_reads_nothing_locally(
    seams: Any, pod: Path, remote: Path, no_local_repo_reads: None
) -> None:
    source = build_file_source(pod)
    assert isinstance(source, ContentBuildFileSource)
    projects = ProjectDiscovery(pod, source).discover()
    assert ("services", "java", "maven", "services/pom.xml") in _key(projects)
    backend, _ = seams
    assert backend.manifest_calls[0][1] == sorted(disc.DISCOVERY_PRUNE_DIRS)
    assert len(backend.manifest_calls) > 1  # paged
    assert ["services/pom.xml"] in backend.fetch_calls  # aggregator via fetch


def test_freshness_reads_nothing_locally(
    seams: Any, pod: Path, no_local_repo_reads: None
) -> None:
    scip_dir = pod / ".code-indexer" / "scip"
    assert scip_db_is_fresh(pod, scip_dir) is False  # no marker yet
    record_scip_build_commit(scip_dir, COMMIT)
    # root pom.xml -> the TOP-LEVEL DB is required
    assert scip_db_is_fresh(pod, scip_dir) is False
    (scip_dir / "index.scip.db").write_bytes(b"db")
    assert scip_db_is_fresh(pod, scip_dir) is True
    _, git = seams
    assert git.calls[0] == (str(pod), ("HEAD",))
    git.commit = "b" * 40
    assert scip_db_is_fresh(pod, scip_dir) is False


def test_root_shape_probe_is_one_fetch_and_cached(seams: Any, pod: Path) -> None:
    backend, git = seams
    assert repo_has_root_build_file(pod) is True
    assert backend.fetch_calls == [list(scip_repo_reads.ROOT_PROBE_NAMES)]
    assert backend.manifest_calls == []  # pom.xml found by name
    assert repo_has_root_build_file(pod) is True
    assert len(backend.fetch_calls) == 1  # cached for this HEAD
    git.commit = "c" * 40
    repo_has_root_build_file(pod)
    assert len(backend.fetch_calls) == 2  # a new commit asks again


def test_root_glob_only_repo_needs_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = tmp_path / "r" / "svc"
    pod = tmp_path / "p" / "svc"
    _write(remote, "App.sln")
    pod.mkdir(parents=True)
    backend = _SeamBackend(pod, remote)
    monkeypatch.setattr(
        content_backend_wiring, "server_content_backend", lambda: backend
    )
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: _GitMcp()
    )
    scip_repo_reads.reset_cache()
    assert repo_has_root_build_file(pod) is True
    assert backend.manifest_calls


def test_golden_repo_reconcile_shape_check_uses_the_seam(seams: Any, pod: Path) -> None:
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    assert GoldenRepoManager._has_root_build_file(pod) is True
    backend, _ = seams
    assert backend.fetch_calls


def test_funnel_dedupes_on_the_git_mcp_commit(
    seams: Any, pod: Path, no_local_repo_reads: None
) -> None:
    builds: List[Any] = []

    class _Q:
        def enqueue(self, build: Any) -> bool:
            builds.append(build)
            return True

    out = funnel_scip_generation(
        alias="svc",
        repo_root=pod,
        config=type("C", (), {"scip_backend": "delegated-plugin"})(),
        golden_repo_manager=object(),
        build_queue=_Q(),  # type: ignore[arg-type]
        generator_builder=lambda *a, **k: type("G", (), {"generate": lambda s: None})(),
    )
    assert out is ScipDelegationOutcome.ENQUEUED
    assert builds[0].dedupe_key == f"svc@{COMMIT}"


def test_backfill_eligibility_discovers_through_the_seam(
    seams: Any, pod: Path, no_local_repo_reads: None
) -> None:
    from code_indexer.server.services.scip_backfill_policy import ScipBackfillPolicy

    policy = ScipBackfillPolicy(object(), {"go"}, object())
    assert policy.eligibility(pod) == {"alias": "svc"}


def test_generator_discovers_through_the_seam(
    seams: Any, pod: Path, no_local_repo_reads: None
) -> None:
    """build_scip_generator hands the generator the content-seam source, and a
    whole generate() discovers every project without touching the pod's repo."""
    from code_indexer.scip.indexers.base import IndexerResult, IndexerStatus
    from code_indexer.server.startup.scip_backend_wiring import build_scip_generator

    seen: List[str] = []

    class _Indexer:
        def generate(
            self, project_dir: Path, output_dir: Path, build_system: str, **kw: Any
        ) -> IndexerResult:
            seen.append(Path(project_dir).relative_to(pod).as_posix())
            return IndexerResult(IndexerStatus.FAILED, 0.0, None, "", "x", 1)

    gen = build_scip_generator(type("C", (), {"scip_backend": "local"})(), pod)
    assert isinstance(gen._build_file_source, ContentBuildFileSource)
    gen._indexers = {lang: _Indexer() for lang in gen._indexers}  # type: ignore[misc]
    result = gen.generate()
    assert result.total_projects == len(
        ProjectDiscovery(pod, build_file_source(pod)).discover()
    )
    assert "tools" in seen and "services" in seen and "jvm" in seen


# ── fail loud ────────────────────────────────────────────────────────────────


def test_delegated_content_with_local_git_fails_loud(
    seams: Any, pod: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: None
    )
    with pytest.raises(ScipRepoReadError, match="git_backend is local"):
        head_commit(pod, local_reader=lambda p: "never")
    with pytest.raises(ScipRepoReadError):
        scip_db_is_fresh(pod, pod / ".code-indexer" / "scip")


@pytest.mark.parametrize("commit,raise_", [("", None), (COMMIT, RuntimeError("down"))])
def test_unresolvable_head_fails_loud(
    seams: Any, pod: Path, commit: str, raise_: Optional[Exception]
) -> None:
    _, git = seams
    git.commit = commit
    git.fail = raise_
    with pytest.raises(ScipRepoReadError):
        head_commit(pod, local_reader=lambda p: "never")


def test_funnel_turns_a_seam_failure_into_a_logged_skip(seams: Any, pod: Path) -> None:
    reset_scip_metrics()
    _, git = seams
    git.fail = RuntimeError("git-mcp down")
    out = funnel_scip_generation(
        alias="svc",
        repo_root=pod,
        config=type("C", (), {"scip_backend": "delegated-plugin"})(),
        golden_repo_manager=object(),
        build_queue=object(),  # type: ignore[arg-type]
        generator_builder=lambda *a, **k: object(),
    )
    assert out is ScipDelegationOutcome.SKIPPED
    assert get_scip_metrics().get("delegated_skipped_error") == 1


def test_content_transport_failure_propagates(seams: Any, pod: Path) -> None:
    backend, _ = seams
    backend.fail = ConnectionError("workspace-mcp down")
    with pytest.raises(ConnectionError):
        ProjectDiscovery(pod, build_file_source(pod)).discover()
    with pytest.raises(ConnectionError):
        repo_has_root_build_file(pod)


def test_a_stuck_manifest_cursor_fails_loud(pod: Path) -> None:
    class _Stuck:
        is_local = False

        def manifest(
            self, repo_path: Any, *, cursor: Any = None, **kw: Any
        ) -> ContentPage:
            return ContentPage(entries=(), next_cursor="same")

        def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
            return iter(())

    with pytest.raises(ScipRepoReadError, match="did not advance"):
        list(ContentBuildFileSource(_Stuck(), pod, pod).iter_files())  # type: ignore[arg-type]


# ── edge cases of the content-seam source ───────────────────────────────────


def test_backend_without_prune_dirs_is_filtered_here(pod: Path, remote: Path) -> None:
    backend = _SeamBackend(pod, remote, prune=False)
    files = [
        p.relative_to(pod).as_posix()
        for p in ContentBuildFileSource(backend, pod, pod).iter_files()
    ]
    assert ".code-indexer/package.json" not in files
    assert "web/package.json" in files
    assert backend.manifest_calls[0][1] is None


def test_unreadable_pom_is_not_an_aggregator_like_local(
    pod: Path, remote: Path
) -> None:
    class _NoRead(_SeamBackend):
        def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
            for rel in paths:
                yield FetchedFile(path=rel, blob_sha=None, data=b"", error="EACCES")

    source = ContentBuildFileSource(_NoRead(pod, remote), pod, pod)
    assert source.read_text(pod / "services" / "pom.xml") is None
    assert source.read_text(Path("/elsewhere/pom.xml")) is None
    assert source.exists(Path("/elsewhere/pom.xml")) is False
    assert disc._is_maven_aggregator(pod / "services" / "pom.xml", source) is False


def test_exists_after_a_walk_uses_the_listing(seams: Any, pod: Path) -> None:
    backend, _ = seams
    source = ContentBuildFileSource(backend, pod, pod)
    list(source.iter_files())
    assert source.exists(pod / "jvm" / "settings.gradle") is True
    assert source.exists(pod / "pom.xml") is True
    assert source.exists(pod / "go.mod") is False
    assert backend.fetch_calls == []  # answered from the listing


def test_unreadable_root_entry_still_exists(pod: Path, remote: Path) -> None:
    class _Dir(_SeamBackend):
        def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
            for rel in paths:
                err = "EISDIR" if rel == "pom.xml" else "ENOENT"
                yield FetchedFile(path=rel, blob_sha=None, data=b"", error=err)

    source = ContentBuildFileSource(_Dir(pod, remote), pod, pod)
    assert source.exists(pod / "pom.xml") is True
    assert source.exists(pod / "go.mod") is False
