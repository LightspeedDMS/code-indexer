"""SCIP context lines through the content seam on a pod without the repository.

On a mount-less pod (``content_backend: delegated``) ``<repo>`` holds only the
index state (``<repo>/.code-indexer``); the repository's files live with the
content backend (workspace-mcp) at the same absolute path. ``DatabaseBackend``
must then fetch each result's source line through that seam -- reading the
pod's directory would silently give every SCIP result ``context=None``.

Every delegated test runs with :func:`_forbid_pod_repo_reads`: any read or
listing of the pod's repository directory outside its index state raises, so
a regression cannot pass by quietly reading the pod. Parity: the delegated
answers equal the local ones over the same tree.

Also here: ``drop_git_backed_documents`` decides lexically, without touching
the pod, when the repository is not on this machine.
"""

from __future__ import annotations

import builtins
import logging
import os
import pathlib
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest

from cidx_scip import generator as generator_mod
from cidx_scip.database.schema import DatabaseManager
from cidx_scip.discovery import LocalBuildFileSource
from cidx_scip.generator import SCIPGenerator, drop_git_backed_documents
from cidx_scip.protobuf import scip_pb2
from cidx_scip.query import backends as backends_mod
from cidx_scip.query.primitives import SCIPQueryEngine
from code_indexer.server.services import file_service
from code_indexer.server.startup import content_backend_wiring
from code_indexer.server.storage.shared.content_backend import (
    FetchedFile,
    LocalContentBackend,
)
from code_indexer.utils.path_confinement import lexical_relative_path

SYMBOL = "python example 1.0 example/ExampleService#"
DEFINITION_ROLE = 1
BACKENDS_LOGGER = "cidx_scip.query.backends"
REPO = "svc"
SNAPSHOT_LEAF = "v_1760000000"

#: The repository as workspace-mcp holds it: path -> text.
TREE = {
    "pkg/service.py": "# header\nclass ExampleService:\n    pass\n",
    "pkg/other.py": "x = 1\ny = 2\nclass ExampleService:  # again\n",
    "services/backend/app.py": "class ExampleService:  # backend\n",
    "latin1.py": "caf\xe9 = 1\n",
}

requires_proc_fd = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="local SCIP context verification reads /proc/self/fd (Linux only)",
)


# ── the database ─────────────────────────────────────────────────────────────


def _build_db(scip_dir: Path, definitions: Sequence[Tuple[str, int]]) -> Path:
    """A real .scip.db under ``scip_dir`` with one definition of SYMBOL per
    ``(stored relative_path, 0-based line)``."""
    scip_dir.mkdir(parents=True, exist_ok=True)
    manager = DatabaseManager(scip_dir / "index.scip")
    manager.create_schema()
    manager.create_indexes()
    db_path = Path(str(scip_dir / "index.scip") + ".db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO symbols (id, name, display_name, kind) VALUES (1, ?, ?, ?)",
            (SYMBOL, "ExampleService", "Class"),
        )
        for doc_id, (relative_path, line) in enumerate(definitions, start=1):
            conn.execute(
                "INSERT INTO documents (id, relative_path, language) VALUES (?, ?, ?)",
                (doc_id, relative_path, "python"),
            )
            conn.execute(
                "INSERT INTO occurrences (id, symbol_id, document_id, start_line, "
                "start_char, end_line, end_char, role) VALUES (?, 1, ?, ?, 0, ?, 5, ?)",
                (doc_id, doc_id, line, line, DEFINITION_ROLE),
            )
        conn.execute("INSERT INTO symbols_fts(symbols_fts) VALUES('rebuild')")
        conn.commit()
    finally:
        conn.close()
    return db_path


def _contexts(db_path: Path) -> Dict[Tuple[str, int], Optional[str]]:
    results = SCIPQueryEngine(db_path).find_definition(SYMBOL, exact=True)
    return {(r.file_path, r.line): r.context for r in results}


# ── the pod, the repository elsewhere, the seam ──────────────────────────────


def _write_tree(root: Path, tree: Dict[str, str], encoding: str = "utf-8") -> None:
    for rel, text in tree.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("latin-1" if rel == "latin1.py" else encoding))


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """The repository where the content backend sees it."""
    root = tmp_path / "remote" / REPO
    _write_tree(root, TREE)
    return root


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The pod's golden-repos directory, with a registry that knows ``svc``."""
    from code_indexer.server.repositories import golden_repo_manager as grm

    base = tmp_path / "pod" / "golden-repos"
    (base / REPO).mkdir(parents=True)
    manager = SimpleNamespace(
        get_golden_repo=lambda name: (
            SimpleNamespace(clone_path=str(base / REPO), repo_url="https://x/svc")
            if name == REPO
            else None
        )
    )
    monkeypatch.setattr(grm, "get_golden_repo_manager", lambda: manager)
    return base


class _Seam:
    """A NON-local content backend: serves ``remote`` when asked about a pod
    path it knows, records every fetch, and can fail or answer errors."""

    is_local = False

    def __init__(self, roots: Dict[Path, Path]) -> None:
        self.roots = roots
        self._local = LocalContentBackend()
        self.fetch_calls: List[Tuple[str, List[str]]] = []
        self.fail: Optional[Exception] = None
        self.errors: Dict[str, str] = {}
        self.unanswered: set = set()

    def manifest(self, *a: Any, **kw: Any) -> Any:
        raise AssertionError("SCIP context needs no manifest")

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        self.fetch_calls.append((str(repo_path), list(paths)))
        if self.fail is not None:
            raise self.fail
        where = self.roots[Path(repo_path)]
        for fetched in self._local.fetch(where, list(paths)):
            if fetched.path in self.unanswered:
                continue
            if fetched.path in self.errors:
                yield FetchedFile(fetched.path, None, b"", self.errors[fetched.path])
            else:
                yield fetched


def _delegate(monkeypatch: pytest.MonkeyPatch, seam: Any) -> None:
    monkeypatch.setattr(content_backend_wiring, "server_content_backend", lambda: seam)


#: Calls that LIST a directory: refused on the repository directory itself.
_LISTINGS = frozenset({"glob", "rglob", "iterdir", "walk", "scandir", "listdir"})


def _forbid_pod_repo_reads(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """Any read under the pod's *repo* outside ``.code-indexer``, or a listing
    of *repo* itself, raises. The directory's own metadata (``stat``, as
    ``resolve`` asks) reads no content and is allowed: it is index state."""
    index_state = repo / ".code-indexer"

    def guard(path: Any, name: str) -> None:
        try:
            p = Path(os.fspath(path)).absolute()
        except TypeError:
            return
        if p == repo:
            if name in _LISTINGS:
                raise AssertionError(f"{name} lists the pod repository: {p}")
            return
        if repo in p.parents and not (p == index_state or index_state in p.parents):
            raise AssertionError(f"pod repository read on a mount-less pod: {p}")

    for name in (
        "exists",
        "is_file",
        "is_dir",
        "read_text",
        "read_bytes",
        "open",
        "glob",
        "rglob",
        "iterdir",
        "stat",
    ):
        original = getattr(pathlib.Path, name)

        def checked(
            self: Any, *a: Any, _o: Any = original, _n: str = name, **kw: Any
        ) -> Any:
            guard(self, _n)
            return _o(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, name, checked)

    def wrap(owner: Any, name: str) -> None:
        original = getattr(owner, name)

        def checked(target: Any = ".", *a: Any, **kw: Any) -> Any:
            if isinstance(target, (str, os.PathLike)):
                guard(target, name)
            return original(target, *a, **kw)

        monkeypatch.setattr(owner, name, checked)

    for owner, name in (
        (builtins, "open"),
        (os, "open"),
        (os, "stat"),
        (os, "walk"),
        (os, "scandir"),
        (os, "listdir"),
    ):
        wrap(owner, name)


def _pod_db(golden: Path, definitions: Sequence[Tuple[str, int]]) -> Path:
    return _build_db(golden / REPO / ".code-indexer" / "scip", definitions)


DEFINITIONS = [
    ("pkg/service.py", 1),
    ("pkg/other.py", 2),
    ("latin1.py", 0),
    ("pkg/service.py", 99),  # past the end: no context, as locally
]


# ── parity ───────────────────────────────────────────────────────────────────


@requires_proc_fd
def test_delegated_context_equals_local_and_reads_nothing_on_the_pod(
    golden: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = _contexts(_build_db(remote / ".code-indexer" / "scip", DEFINITIONS))
    assert local[("pkg/service.py", 1)] == "class ExampleService:"

    seam = _Seam({golden / REPO: remote})
    db_path = _pod_db(golden, DEFINITIONS)
    _delegate(monkeypatch, seam)
    _forbid_pod_repo_reads(monkeypatch, golden / REPO)

    assert _contexts(db_path) == local
    # One fetch, asked at the repository's own path, each file once.
    assert len(seam.fetch_calls) == 1
    root, paths = seam.fetch_calls[0]
    assert root == str(golden / REPO)
    assert sorted(paths) == ["latin1.py", "pkg/other.py", "pkg/service.py"]


def test_guard_is_live(golden: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (golden / REPO / "pkg").mkdir()
    (golden / REPO / "pkg" / "service.py").write_text("stale\n")
    _forbid_pod_repo_reads(monkeypatch, golden / REPO)
    probes = [
        lambda: (golden / REPO / "pkg" / "service.py").read_text(),
        lambda: open(golden / REPO / "pkg" / "service.py"),
        lambda: os.open(golden / REPO / "pkg" / "service.py", os.O_RDONLY),
    ]
    for probe in probes:
        with pytest.raises(AssertionError, match="pod repository read"):
            probe()
    with pytest.raises(AssertionError, match="lists the pod repository"):
        os.listdir(golden / REPO)


@requires_proc_fd
def test_monorepo_subproject_paths_are_asked_from_the_repository_root(
    golden: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _Seam({golden / REPO: remote})
    db_path = _build_db(
        golden / REPO / ".code-indexer" / "scip" / "services" / "backend",
        [("app.py", 0)],
    )
    _delegate(monkeypatch, seam)
    _forbid_pod_repo_reads(monkeypatch, golden / REPO)

    assert _contexts(db_path) == {("app.py", 0): "class ExampleService:  # backend"}
    assert seam.fetch_calls == [(str(golden / REPO), ["services/backend/app.py"])]


def test_versioned_snapshot_is_read_from_the_base_clone(
    golden: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = golden / ".versioned" / REPO / SNAPSHOT_LEAF
    seam = _Seam({golden / REPO: remote})
    db_path = _build_db(snapshot / ".code-indexer" / "scip", [("pkg/service.py", 1)])
    _delegate(monkeypatch, seam)
    _forbid_pod_repo_reads(monkeypatch, snapshot)
    _forbid_pod_repo_reads(monkeypatch, golden / REPO)

    assert _contexts(db_path) == {("pkg/service.py", 1): "class ExampleService:"}
    assert [root for root, _ in seam.fetch_calls] == [str(golden / REPO)]


def test_seam_is_asked_at_the_unresolved_repository_path(
    golden: Path, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry and workspace-mcp know the repository by the path cidx
    uses, not by wherever a symlink on this pod resolves it to."""
    linked = tmp_path / "data-link"
    linked.symlink_to(golden.parent)
    pod_repo = linked / golden.name / REPO
    seam = _Seam({pod_repo: remote})
    db_path = _build_db(pod_repo / ".code-indexer" / "scip", [("pkg/service.py", 1)])
    _delegate(monkeypatch, seam)
    _forbid_pod_repo_reads(monkeypatch, golden / REPO)

    assert _contexts(db_path) == {("pkg/service.py", 1): "class ExampleService:"}
    assert [root for root, _ in seam.fetch_calls] == [str(pod_repo)]


def test_stored_paths_naming_one_file_share_one_fetch(
    golden: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _Seam({golden / REPO: remote})
    db_path = _pod_db(golden, [("pkg/service.py", 1), ("pkg/sub/../service.py", 1)])
    _delegate(monkeypatch, seam)

    assert _contexts(db_path) == {
        ("pkg/service.py", 1): "class ExampleService:",
        ("pkg/sub/../service.py", 1): "class ExampleService:",
    }
    assert seam.fetch_calls == [(str(golden / REPO), ["pkg/service.py"])]


def test_files_are_fetched_in_bounded_batches(
    golden: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = backends_mod.CONTENT_SEAM_FETCH_BATCH_FILES + 3
    remote = tmp_path / "many"
    _write_tree(remote, {f"m/f{i:03}.py": f"line {i}\n" for i in range(count)})
    seam = _Seam({golden / REPO: remote})
    db_path = _pod_db(golden, [(f"m/f{i:03}.py", 0) for i in range(count)])
    _delegate(monkeypatch, seam)
    _forbid_pod_repo_reads(monkeypatch, golden / REPO)

    contexts = _contexts(db_path)

    assert contexts == {(f"m/f{i:03}.py", 0): f"line {i}" for i in range(count)}
    assert [len(paths) for _, paths in seam.fetch_calls] == [
        backends_mod.CONTENT_SEAM_FETCH_BATCH_FILES,
        3,
    ]


def test_seam_is_resolved_once_per_backend(
    golden: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _Seam({golden / REPO: remote})
    db_path = _pod_db(golden, [("pkg/service.py", 1)])
    _delegate(monkeypatch, seam)
    asked: List[str] = []
    real = file_service.content_seam_source

    def counting(repo_path: str, **kw: Any) -> Any:
        asked.append(repo_path)
        return real(repo_path, **kw)

    monkeypatch.setattr(file_service, "content_seam_source", counting)
    engine = SCIPQueryEngine(db_path)
    engine.find_definition(SYMBOL, exact=True)
    engine.find_definition(SYMBOL, exact=True)

    assert asked == [str(golden / REPO)]
    assert len(seam.fetch_calls) == 2


# ── confinement: decided lexically, never fetched ────────────────────────────


@pytest.mark.parametrize(
    "stored",
    [
        "../outside.py",
        "pkg/../../outside.py",
        ".git/config",
        "pkg/../.GIT/config",
        "/etc/passwd",
        "bad\x00name.py",
    ],
)
def test_out_of_repository_paths_are_refused_without_a_fetch(
    stored: str,
    golden: Path,
    remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seam = _Seam({golden / REPO: remote})
    db_path = _pod_db(golden, [(stored, 0)])
    _delegate(monkeypatch, seam)
    _forbid_pod_repo_reads(monkeypatch, golden / REPO)

    with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
        assert list(_contexts(db_path).values()) == [None]

    assert seam.fetch_calls == []
    assert "do not resolve inside repository root" in caplog.text


# ── soft failures ────────────────────────────────────────────────────────────


def test_missing_file_has_no_context_and_no_warning(
    golden: Path,
    remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seam = _Seam({golden / REPO: remote})
    db_path = _pod_db(golden, [("gone.py", 0), ("pkg/service.py", 1)])
    _delegate(monkeypatch, seam)

    with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
        contexts = _contexts(db_path)

    assert contexts == {
        ("gone.py", 0): None,
        ("pkg/service.py", 1): "class ExampleService:",
    }
    assert caplog.records == []


@pytest.mark.parametrize("how", ["error", "no record"])
def test_a_file_the_backend_cannot_serve_is_one_warning(
    how: str,
    golden: Path,
    remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seam = _Seam({golden / REPO: remote})
    if how == "error":
        seam.errors["pkg/other.py"] = "EIO"
    else:
        seam.unanswered.add("pkg/other.py")
    db_path = _pod_db(golden, [("pkg/other.py", 2), ("pkg/service.py", 1)])
    _delegate(monkeypatch, seam)

    with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
        contexts = _contexts(db_path)

    assert contexts == {
        ("pkg/other.py", 2): None,
        ("pkg/service.py", 1): "class ExampleService:",
    }
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1
    assert "1 file(s) the content backend could not serve" in warnings[0]


def test_an_unreachable_backend_is_one_distinct_warning_and_asked_once(
    golden: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    count = backends_mod.CONTENT_SEAM_FETCH_BATCH_FILES + 1
    seam = _Seam({golden / REPO: tmp_path})
    seam.fail = ConnectionError("workspace-mcp unreachable")
    db_path = _pod_db(golden, [(f"f{i}.py", 0) for i in range(count)])
    _delegate(monkeypatch, seam)
    _forbid_pod_repo_reads(monkeypatch, golden / REPO)

    with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
        contexts = _contexts(db_path)

    assert set(contexts.values()) == {None}
    assert len(seam.fetch_calls) == 1  # a down backend is not asked again
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1
    assert f"{count} file(s): the content backend is unavailable" in warnings[0]
    assert "ConnectionError" in warnings[0]


def test_a_backend_failing_mid_batch_counts_only_unanswered_files(
    golden: Path,
    remote: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _DiesAfterOne(_Seam):
        def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
            produced = super().fetch(repo_path, paths)
            yield next(produced)
            raise TimeoutError("read timed out")

    seam = _DiesAfterOne({golden / REPO: remote})
    db_path = _pod_db(golden, [("pkg/service.py", 1), ("pkg/other.py", 2)])
    _delegate(monkeypatch, seam)

    with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
        contexts = _contexts(db_path)

    # The file answered before the failure keeps its context; the other not.
    first = seam.fetch_calls[0][1][0]
    expected = {
        ("pkg/service.py", 1): "class ExampleService:",
        ("pkg/other.py", 2): "class ExampleService:  # again",
    }
    assert contexts == {
        key: (line if key[0] == first else None) for key, line in expected.items()
    }
    assert "1 file(s): the content backend is unavailable" in caplog.text


def test_seam_resolution_failure_is_the_unavailable_warning(
    golden: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(repo_path: str, **kw: Any) -> Any:
        raise RuntimeError("registry down")

    db_path = _pod_db(golden, [("pkg/service.py", 1)])
    _delegate(monkeypatch, _Seam({}))
    monkeypatch.setattr(file_service, "content_seam_source", broken)
    _forbid_pod_repo_reads(monkeypatch, golden / REPO)

    with caplog.at_level(logging.WARNING, logger=BACKENDS_LOGGER):
        assert list(_contexts(db_path).values()) == [None]

    assert "the content backend is unavailable (RuntimeError" in caplog.text


# ── the local default is unchanged ───────────────────────────────────────────


@requires_proc_fd
@pytest.mark.parametrize("server_backend", [None, LocalContentBackend()])
def test_local_content_reads_the_disk_without_the_seam(
    server_backend: Any, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        content_backend_wiring, "server_content_backend", lambda: server_backend
    )

    # The seam is always asked; its local implementation answers with this
    # pod's directory, which is read as before (nothing is fetched).
    answered: List[Any] = []
    real = file_service.content_seam_source

    def recording(*a: Any, **kw: Any) -> Any:
        answered.append(real(*a, **kw))
        return answered[-1]

    monkeypatch.setattr(file_service, "content_seam_source", recording)
    db_path = _build_db(remote / ".code-indexer" / "scip", [("pkg/service.py", 1)])

    assert _contexts(db_path) == {("pkg/service.py", 1): "class ExampleService:"}
    assert answered and all(
        file_service.pod_directory(source) is not None for source in answered
    )


# ── lexical_relative_path ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "candidate, expected",
    [
        ("/r/a/b.py", "a/b.py"),
        ("/r/a/../b.py", "b.py"),
        ("/r/./a//b.py", "a/b.py"),
        ("/r", None),
        ("/r/../x.py", None),
        ("/rx/a.py", None),
        ("/elsewhere/a.py", None),
        ("/r/a\x00b", None),
    ],
)
def test_lexical_relative_path(candidate: str, expected: Optional[str]) -> None:
    assert lexical_relative_path(Path(candidate), Path("/r")) == expected


# ── drop_git_backed_documents without the repository ─────────────────────────


def _new_index() -> Any:
    return getattr(scip_pb2, "Index")()


def _scip_index(path: Path, relative_paths: Sequence[str]) -> Path:
    index = _new_index()
    for rel in relative_paths:
        index.documents.add(relative_path=rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(index.SerializeToString())
    return path


def _kept(path: Path) -> List[str]:
    index = _new_index()
    index.ParseFromString(path.read_bytes())
    return [doc.relative_path for doc in index.documents]


DOCS = ["a.py", ".git/config", "sub/../.git/HEAD", "../elsewhere.py", "b.py"]


def test_drop_git_backed_documents_is_lexical_off_the_machine(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = golden / REPO
    scip_file = _scip_index(repo / ".code-indexer" / "scip" / "index.scip", DOCS)
    _forbid_pod_repo_reads(monkeypatch, repo)
    resolved: List[Any] = []
    real_resolve = pathlib.Path.resolve

    def recording_resolve(self: Any, *a: Any, **kw: Any) -> Any:
        resolved.append(self)
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "resolve", recording_resolve)

    removed = drop_git_backed_documents(
        scip_file, repo, repo, repository_on_this_machine=False
    )

    assert removed == 2
    assert _kept(scip_file) == ["a.py", "../elsewhere.py", "b.py"]
    assert resolved == []  # nothing of the repository resolved on the pod


def test_drop_git_backed_documents_local_parity(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    scip_file = _scip_index(repo / ".code-indexer" / "scip" / "index.scip", DOCS)

    assert drop_git_backed_documents(scip_file, repo, repo) == 2
    assert _kept(scip_file) == ["a.py", "../elsewhere.py", "b.py"]


def test_drop_git_backed_documents_local_still_judges_symlinks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[core]\n")
    (repo / "link.py").symlink_to(repo / ".git" / "config")
    scip_file = _scip_index(
        repo / ".code-indexer" / "scip" / "index.scip", ["link.py", "a.py"]
    )

    assert drop_git_backed_documents(scip_file, repo, repo) == 1
    assert _kept(scip_file) == ["a.py"]


class _ElsewhereSource:
    """A non-local BuildFileSource (as ``ContentBuildFileSource`` is) over a
    tree that is not under the generator's repository root."""

    def __init__(self, pod: Path, remote: Path) -> None:
        self._pod = pod
        self._remote = remote
        self._local = LocalBuildFileSource(remote)

    def _there(self, path: Path) -> Path:
        return self._remote / Path(path).relative_to(self._pod)

    def iter_files(self) -> Iterator[Path]:
        for path in self._local.iter_files():
            yield self._pod / path.relative_to(self._remote)

    def exists(self, path: Path) -> bool:
        return self._local.exists(self._there(path))

    def root_glob(self, pattern: str) -> bool:
        return self._local.root_glob(pattern)

    def read_text(self, path: Path) -> Optional[str]:
        return self._local.read_text(self._there(path))


class _WritesIndex:
    """An indexer that succeeds, writing an index with a .git-backed document."""

    def generate(self, project_dir: Path, output_dir: Path, *a: Any, **kw: Any) -> Any:
        from cidx_scip.indexers.base import IndexerResult, IndexerStatus

        out = _scip_index(Path(output_dir) / "index.scip", ["a.py", ".git/config"])
        return IndexerResult(IndexerStatus.SUCCESS, 0.0, out, "", "", 0)


@pytest.mark.parametrize("where", ["here", "elsewhere"])
def test_generator_drops_git_documents_wherever_the_repository_is(
    where: str,
    golden: Path,
    remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (remote / "go.mod").write_text("module x\n")
    seen: List[bool] = []
    real_drop = generator_mod.drop_git_backed_documents

    def recording(*a: Any, **kw: Any) -> int:
        seen.append(kw["repository_on_this_machine"])
        return real_drop(*a, **kw)

    monkeypatch.setattr(generator_mod, "drop_git_backed_documents", recording)
    files: Any = None
    repo = remote
    if where == "elsewhere":
        repo = golden / REPO
        files = _ElsewhereSource(repo, remote)
        _forbid_pod_repo_reads(monkeypatch, repo)
    indexers: Dict[str, Any] = {"go": _WritesIndex()}
    generator = SCIPGenerator(repo, indexer_map=indexers, build_file_source=files)

    result = generator.generate()

    assert result.successful_projects == 1
    assert seen == [where == "here"]
    assert _kept(repo / ".code-indexer" / "scip" / "index.scip") == ["a.py"]


# ── issue-64616: indexes built by the delegated plugin (sandbox root) ──────────

SANDBOX_ROOT = "file:///workspace"


def _plugin_shaped_db(scip_dir: Path, definitions: Sequence[Tuple[str, int]]) -> Path:
    """A database plus the ``index.scip`` beside it, as a plugin build leaves
    them: the protobuf's project root is the SANDBOX build directory."""
    db_path = _build_db(scip_dir, definitions)
    index = _new_index()
    index.metadata.project_root = SANDBOX_ROOT
    index.metadata.tool_info.name = "scip-typescript"
    (scip_dir / "index.scip").write_bytes(index.SerializeToString())
    return db_path


def _results(db_path: Path) -> List[Any]:
    return SCIPQueryEngine(db_path).find_definition(SYMBOL, exact=True)


@requires_proc_fd
def test_local_plugin_shaped_index_has_context_and_reports_the_repository(
    remote: Path,
) -> None:
    db_path = _plugin_shaped_db(
        remote / ".code-indexer" / "scip", [("pkg/service.py", 1)]
    )

    (result,) = _results(db_path)

    assert result.context == "class ExampleService:"
    assert result.project == str(remote), "the sandbox path leaked into results"


@requires_proc_fd
def test_local_plugin_shaped_subproject_resolves_under_its_offset(
    remote: Path,
) -> None:
    db_path = _plugin_shaped_db(
        remote / ".code-indexer" / "scip" / "services" / "backend", [("app.py", 0)]
    )

    (result,) = _results(db_path)

    assert result.context == "class ExampleService:  # backend"
    assert result.project == str(remote / "services" / "backend")


def test_delegated_plugin_shaped_index_reads_through_the_seam(
    golden: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _Seam({golden / REPO: remote})
    db_path = _plugin_shaped_db(
        golden / REPO / ".code-indexer" / "scip" / "services" / "backend",
        [("app.py", 0), ("../../../escape.py", 0)],
    )
    _delegate(monkeypatch, seam)
    _forbid_pod_repo_reads(monkeypatch, golden / REPO)

    contexts = {(r.file_path, r.project): r.context for r in _results(db_path)}

    backend_dir = str(golden / REPO / "services" / "backend")
    assert contexts == {
        ("app.py", backend_dir): "class ExampleService:  # backend",
        ("../../../escape.py", backend_dir): None,
    }
    assert seam.fetch_calls == [(str(golden / REPO), ["services/backend/app.py"])]


def test_the_plugin_build_records_the_projects_own_directory(tmp_path: Path) -> None:
    from cidx_scip.indexers.plugin_backend import _with_project_root

    index = _new_index()
    index.metadata.project_root = SANDBOX_ROOT
    index.documents.add(relative_path="src/assert.ts")
    project_dir = tmp_path / "golden-repos" / "svc" / "web"

    rewritten = _new_index()
    rewritten.ParseFromString(
        _with_project_root(index.SerializeToString(), project_dir)
    )

    assert rewritten.metadata.project_root == project_dir.as_uri()
    assert [d.relative_path for d in rewritten.documents] == ["src/assert.ts"]
    assert _with_project_root(b"\xff not protobuf", project_dir) == b"\xff not protobuf"
