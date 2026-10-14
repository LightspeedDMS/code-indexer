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
from code_indexer.server.services import scip_lifecycle as scip_lifecycle_mod
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
        #: pod path -> where the backend really finds that repository.
        self.roots = {Path(pod): Path(remote)}
        self._local = LocalContentBackend()
        self.manifest_calls: List[Tuple[Optional[str], Any]] = []
        self.fetch_calls: List[List[str]] = []
        self.fail: Optional[Exception] = None
        if not prune:
            self.manifest = self._manifest_without_prune  # type: ignore[method-assign]

    def _where(self, repo_path: Any) -> Path:
        assert Path(repo_path) in self.roots, repo_path
        return self.roots[Path(repo_path)]

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


#: How many manifest pages a looping fake serves before it gives up. The loop
#: guard must stop well before this; a missing guard then fails here, at once,
#: instead of paging forever (smoke has no per-test timeout).
_PAGE_BUDGET = 10


class _LoopingManifest:
    """Hands out cursors from ``cycle`` forever -- the guard must stop it."""

    is_local = False

    def __init__(self, cycle: Sequence[str]) -> None:
        self._cycle = list(cycle)
        self.calls = 0

    def manifest(self, repo_path: Any, *, cursor: Any = None, **kw: Any) -> ContentPage:
        self.calls += 1
        if self.calls > _PAGE_BUDGET:
            raise AssertionError(f"manifest paged {self.calls} times: no loop guard")
        return ContentPage(
            entries=(), next_cursor=self._cycle[(self.calls - 1) % len(self._cycle)]
        )

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        return iter(())


@pytest.mark.parametrize(
    "cycle,max_calls", [(["same"], 2), (["a", "b"], 3), (["a", "b", "c"], 4)]
)
def test_a_looping_manifest_cursor_fails_loud_and_fast(
    pod: Path, cycle: List[str], max_calls: int
) -> None:
    backend = _LoopingManifest(cycle)
    with pytest.raises(ScipRepoReadError, match="did not advance"):
        list(ContentBuildFileSource(backend, pod, pod).iter_files())  # type: ignore[arg-type]
    assert backend.calls == max_calls


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


# ── the SCIP backfill reconcile on a mount-less pod ──────────────────────────


def _reconcile_env(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, str]]:
    from unittest import mock

    calls: List[Tuple[str, str]] = []

    def funnel(alias: str, repo_root: str, config: Any) -> Any:
        calls.append((alias, str(repo_root)))
        return ScipDelegationOutcome.ENQUEUED

    monkeypatch.setattr(scip_lifecycle_mod, "funnel_golden_scip", funnel)
    cfg = mock.MagicMock()
    cfg.get_config.return_value = object()
    monkeypatch.setattr(
        "code_indexer.server.services.config_service.get_config_service", lambda: cfg
    )
    monkeypatch.setenv("CIDX_SCIP_AUTO_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("CIDX_SCIP_LANGUAGES", "go,python,typescript,java")
    return calls


def test_scip_reconcile_scans_the_registry_not_the_pod_directory(
    seams: Any,
    pod: Path,
    remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a pod holding only index state, the pod's directory is the wrong list:
    it lacks repos registered elsewhere and keeps dirs of removed ones. The
    reconcile must scan the registry -- and read no repository file locally."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    backend, _ = seams
    golden = pod.parent
    other_remote = tmp_path / "remote" / "other"
    _write(other_remote, "go.mod", "module other")
    backend.roots[golden / "other"] = other_remote  # registered on another pod
    (golden / "stale" / ".code-indexer").mkdir(parents=True)  # removed repo
    (golden / "cidx-meta").mkdir()  # local:// -- cidx's own files

    m = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    m.golden_repos_dir = str(golden)
    m._content_backend = backend  # type: ignore[assignment]
    registry = [
        {"alias": "svc", "repo_url": "https://x/svc.git", "clone_path": str(pod)},
        {
            "alias": "cidx-meta",
            "repo_url": "local://cidx-meta",
            "clone_path": str(golden / "cidx-meta"),
        },
        {
            "alias": "other",
            "repo_url": "https://x/other.git",
            "clone_path": str(golden / "other"),
        },
    ]
    monkeypatch.setattr(m, "list_golden_repos", lambda: registry)
    calls = _reconcile_env(monkeypatch)
    _forbid_repo_reads_under(monkeypatch, golden)

    counts = m.reconcile_scip_from_filesystem(max_enqueue=10)

    assert calls == [("other", str(golden / "other")), ("svc", str(pod))]
    assert counts["scanned"] == 2 and counts["enqueued"] == 2 and counts["errors"] == 0


def test_scip_reconcile_with_local_content_scans_the_directory(tmp_path: Path) -> None:
    """Local content seam: the directory scan, exactly as before."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    m = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    assert m._content_backend.is_local
    assert m._scip_backfill_entries() is None


def test_backfill_engine_takes_entries_as_given(tmp_path: Path) -> None:
    """Given entries, the engine neither lists its root nor drops a missing dir,
    and enqueues under the alias it was given -- not the path's last component."""
    from code_indexer.server.services.golden_index_backfill import (
        GoldenIndexBackfillReconciler,
    )

    seen: List[str] = []
    enqueued: List[Tuple[str, str]] = []

    class _Policy:
        name = "t"
        attempt_marker = ".t"

        def is_present(self, repo_path: Path) -> bool:
            seen.append(repo_path.name)
            return repo_path.name == ".a"

        def is_fresh(self, repo_path: Path) -> bool:
            return True

        def eligibility(self, repo_path: Path) -> Any:
            return {}

        def enqueue(self, alias: str, repo_path: Path, meta: Any) -> None:
            enqueued.append((alias, repo_path.name))

    root = tmp_path / "missing-root"
    counts = GoldenIndexBackfillReconciler(root).run_cycle(
        _Policy(),  # type: ignore[arg-type]
        max_enqueue=5,
        cooldown_seconds=0,
        entries=[("svc", tmp_path / "v_7"), ("dot", tmp_path / ".a")],
    )
    assert seen == ["v_7", ".a"]
    assert enqueued == [("svc", "v_7")]
    assert counts.scanned == 2 and counts.present == 1 and counts.enqueued == 1


def test_scip_reconcile_uses_registry_aliases_for_versioned_and_in_place_paths(
    seams: Any,
    pod: Path,
    remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clone path need not end in its alias: the registry's alias is enqueued."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    backend, _ = seams
    golden = pod.parent
    versioned = golden / ".versioned" / "svc" / "v_42"
    in_place = tmp_path / "srv" / "checkout"
    backend.roots = {versioned: remote, in_place: remote}
    monkeypatch.setattr(
        content_backend_wiring, "golden_repo_content_root", lambda p, consumer: Path(p)
    )
    m = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    m.golden_repos_dir = str(golden)
    m._content_backend = backend  # type: ignore[assignment]
    registry = [
        {"alias": "svc", "repo_url": "https://x/svc.git", "clone_path": str(versioned)},
        {
            "alias": "inplace",
            "repo_url": "file:///srv/checkout",
            "clone_path": str(in_place),
        },
    ]
    monkeypatch.setattr(m, "list_golden_repos", lambda: registry)
    calls = _reconcile_env(monkeypatch)

    counts = m.reconcile_scip_from_filesystem(max_enqueue=10)

    assert calls == [("inplace", str(in_place)), ("svc", str(versioned))]
    assert counts["errors"] == 0


# ── fetch errors: absence is an answer, a failed read is not ─────────────────


class _ErrorBackend(_SeamBackend):
    """Fetch answers ``errors[path]`` for the named paths, the real file otherwise."""

    def __init__(self, pod: Path, remote: Path, errors: dict) -> None:
        super().__init__(pod, remote)
        self.errors = dict(errors)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        self.fetch_calls.append(list(paths))
        real = {f.path: f for f in self._local.fetch(self._where(repo_path), paths)}
        for rel in paths:
            if rel in self.errors:
                yield FetchedFile(
                    path=rel, blob_sha=None, data=b"", error=self.errors[rel]
                )
            else:
                yield real[rel]


def _seam(monkeypatch: pytest.MonkeyPatch, backend: Any) -> _GitMcp:
    git = _GitMcp()
    monkeypatch.setattr(
        content_backend_wiring, "server_content_backend", lambda: backend
    )
    monkeypatch.setattr(git_backend_wiring, "server_delegated_git_backend", lambda: git)
    scip_repo_reads.reset_cache()
    return git


def test_root_entry_there_but_not_served_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EFBIG/EISDIR: the file is there, as Path.exists() would say."""
    remote = tmp_path / "r" / "svc"
    pod = tmp_path / "p" / "svc"
    _write(remote, "README.md")
    pod.mkdir(parents=True)
    errors = {n: "ENOENT" for n in scip_repo_reads.ROOT_PROBE_NAMES}
    errors["pom.xml"] = "EFBIG"
    _seam(monkeypatch, _ErrorBackend(pod, remote, errors))
    assert repo_has_root_build_file(pod) is True


@pytest.mark.parametrize("error", ["EIO", "ETIMEDOUT", "unreadable"])
def test_root_probe_failure_raises_and_caches_nothing(
    pod: Path, remote: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    backend = _ErrorBackend(pod, remote, {"settings.gradle": error})
    _seam(monkeypatch, backend)
    with pytest.raises(ScipRepoReadError, match=error):
        repo_has_root_build_file(pod)
    assert scip_repo_reads._root_cache == {}
    backend.errors.clear()  # the blip passes: the next check asks again
    assert repo_has_root_build_file(pod) is True
    assert len(backend.fetch_calls) == 2


@pytest.mark.parametrize("error", ["ENOENT", "EACCES", "ENOTDIR", os.strerror(2)])
def test_not_found_errors_mean_absent_and_are_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    remote = tmp_path / "r" / "svc"
    pod = tmp_path / "p" / "svc"
    _write(remote, "README.md")
    pod.mkdir(parents=True)
    names = scip_repo_reads.ROOT_PROBE_NAMES
    backend = _ErrorBackend(pod, remote, {n: error for n in names})
    _seam(monkeypatch, backend)
    assert repo_has_root_build_file(pod) is False
    assert len(scip_repo_reads._root_cache) == 1
    assert repo_has_root_build_file(pod) is False
    assert len(backend.fetch_calls) == 1


def test_probe_with_a_missing_record_raises(pod: Path, remote: Path) -> None:
    class _Short(_SeamBackend):
        def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
            yield FetchedFile(path=paths[0], blob_sha=None, data=b"", error="ENOENT")

    source = ContentBuildFileSource(_Short(pod, remote), pod, pod)
    with pytest.raises(ScipRepoReadError, match="no record"):
        source.exists(pod / "pom.xml")


@pytest.mark.parametrize(
    "error,expect",
    [
        ("EIO", "raise"),
        ("ETIMEDOUT", "raise"),
        ("EFBIG", "raise"),
        ("ENOENT", None),
        ("EACCES", None),
        ("EISDIR", None),
        (os.strerror(21), None),
    ],
)
def test_read_text_errors(pod: Path, remote: Path, error: str, expect: Any) -> None:
    rel = "services/pom.xml"
    source = ContentBuildFileSource(_ErrorBackend(pod, remote, {rel: error}), pod, pod)
    if expect == "raise":
        with pytest.raises(ScipRepoReadError, match=error):
            source.read_text(pod / rel)
        with pytest.raises(ScipRepoReadError):
            ProjectDiscovery(pod, source).discover()
    else:
        assert source.read_text(pod / rel) is None


def test_read_text_with_no_record_raises(pod: Path, remote: Path) -> None:
    class _Empty(_SeamBackend):
        def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
            return iter(())

    with pytest.raises(ScipRepoReadError, match="no record"):
        ContentBuildFileSource(_Empty(pod, remote), pod, pod).read_text(pod / "pom.xml")


# ── HEAD is asked once per repo per cycle, not once per check ────────────────


def test_freshness_asks_head_once(seams: Any, pod: Path) -> None:
    _, git = seams
    scip_dir = pod / ".code-indexer" / "scip"
    record_scip_build_commit(scip_dir, COMMIT)
    (scip_dir / "index.scip.db").write_bytes(b"db")
    assert scip_db_is_fresh(pod, scip_dir) is True
    assert len(git.calls) == 1  # the root-shape check reused it


def test_head_memo_scopes_one_head_per_repo(seams: Any, pod: Path) -> None:
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    _, git = seams
    scip_dir = pod / ".code-indexer" / "scip"
    record_scip_build_commit(scip_dir, COMMIT)
    (scip_dir / "index.scip.db").write_bytes(b"db")
    with scip_repo_reads.head_memo():
        assert GoldenRepoManager._has_root_build_file(pod) is True
        assert scip_db_is_fresh(pod, scip_dir) is True
        assert GoldenRepoManager._has_root_build_file(pod) is True
    assert len(git.calls) == 1
    git.commit = "d" * 40  # outside the block HEAD is asked again, and seen
    assert scip_db_is_fresh(pod, scip_dir) is False
    assert len(git.calls) == 2


def test_reconcile_cycle_asks_head_once_per_repo(
    seams: Any, pod: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    _, git = seams
    scip_dir = pod / ".code-indexer" / "scip"
    record_scip_build_commit(scip_dir, COMMIT)
    (scip_dir / "index.scip.db").write_bytes(b"db")
    m = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    m.golden_repos_dir = str(pod.parent)
    m._content_backend = seams[0]  # type: ignore[assignment]
    registry = [{"alias": "svc", "repo_url": "https://x", "clone_path": str(pod)}]
    monkeypatch.setattr(m, "list_golden_repos", lambda: registry)
    _reconcile_env(monkeypatch)
    counts = m.reconcile_scip_from_filesystem()
    assert counts["scip_present"] == 1
    assert len(git.calls) == 1


def _forbid_repo_reads_under(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Fail on any read under *root* except index state (``*/.code-indexer``)."""

    def guard(path: Any) -> None:
        try:
            p = Path(os.fspath(path)).absolute()
        except TypeError:
            return
        if (p == root or root in p.parents) and ".code-indexer" not in p.parts:
            raise AssertionError(f"local repository read on a mount-less pod: {p}")

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
        original = getattr(pathlib.Path, name)

        def checked(self: Any, *a: Any, _o: Any = original, **kw: Any) -> Any:
            guard(self)
            return _o(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, name, checked)
    real_walk, real_scandir = os.walk, os.scandir
    monkeypatch.setattr(
        os, "walk", lambda top, *a, **kw: (guard(top), real_walk(top, *a, **kw))[1]
    )
    monkeypatch.setattr(
        os, "scandir", lambda path=".": (guard(path), real_scandir(path))[1]
    )
