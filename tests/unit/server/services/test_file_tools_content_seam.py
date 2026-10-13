"""The golden-repo FILE tools read through the content seam on a mount-less pod.

On a pod configured with a non-local content backend the repository files live
only behind that backend, at the same absolute paths the registry records. The
pod holds index state only: after a refresh an alias points at a pod-local
snapshot ``<golden_repos_dir>/.versioned/<repo>/v_<ts>`` that contains nothing
but ``.code-indexer``. These tests build ONE real repository and run each file
tool's service call twice:

* locally, on the repository path itself (today's code path), and
* "delegated": a fake NON-local backend that wraps ``LocalContentBackend`` over
  the very same tree, with the call made on the snapshot path, which holds only
  ``.code-indexer``.

The two must agree, except for the documented symlink rule: the manifest
reports a symlink as an entry and never follows it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Union

import pytest

from code_indexer.global_repos.directory_explorer import (
    DirectoryExplorerService,
    ManifestDirectoryExplorerService,
    TreeNode,
)
from code_indexer.server.models.api_models import FileListQueryParams
from code_indexer.server.services import file_service as fs_mod
from code_indexer.server.services.config_service import reset_config_service
from code_indexer.server.services.file_service import (
    ContentSeamSource,
    FileListingService,
    content_seam_source,
    directory_explorer_for,
    fetch_one,
    manifest_entries,
    modified_at_from_ns,
)
from code_indexer.server.startup import content_backend_wiring as wiring
from code_indexer.server.storage.shared.content_backend import (
    ContentPage,
    FetchedFile,
    LocalContentBackend,
)
from code_indexer.utils.path_confinement import confine_relative_path

REPO = "example-repo"
SNAPSHOT_LEAF = "v_1760000000"
#: A fixed mtime with a non-zero nanosecond part, so the format is exercised.
MTIME_NS = 1_700_000_000_123_456_789
LINK = "link.py"


class DelegatedBackend:
    """A NON-local backend over the same tree, recording every call."""

    def __init__(self, page_size: Optional[int] = None) -> None:
        self._inner = LocalContentBackend()
        self._page_size = page_size
        self.manifest_calls: List[Dict[str, Any]] = []
        self.fetch_calls: List[List[str]] = []

    @property
    def is_local(self) -> bool:
        return False

    def manifest(
        self,
        repo_path: Union[str, Path],
        *,
        cursor: Optional[str] = None,
        page_size: int = 1000,
        prune_dirs: Optional[Sequence[str]] = None,
    ) -> ContentPage:
        self.manifest_calls.append(
            {"root": str(repo_path), "cursor": cursor, "prune": list(prune_dirs or ())}
        )
        return self._inner.manifest(
            repo_path,
            cursor=cursor,
            page_size=self._page_size or page_size,
            prune_dirs=prune_dirs,
        )

    def fetch(
        self, repo_path: Union[str, Path], paths: Sequence[str]
    ) -> Iterator[FetchedFile]:
        self.fetch_calls.append(list(paths))
        return self._inner.fetch(repo_path, paths)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated(tmp_path_factory, monkeypatch):
    """A private server config and an empty listing cache for every test."""
    data_dir = tmp_path_factory.mktemp("server-data")
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(data_dir))
    reset_config_service()
    fs_mod._collect_cache.clear()
    yield
    fs_mod._collect_cache.clear()
    reset_config_service()


def _write(path: Path, data: Union[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_bytes(data.encode("utf-8"))


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """golden-repos/ holding one real repository, its snapshot, and a registry."""
    from code_indexer.server.repositories import golden_repo_manager as grm

    base = tmp_path / "golden-repos"
    repo = base / REPO
    _write(repo / "README.md", "# Example\n\nhello\n")
    _write(repo / "src" / "app.py", "def main():\n    return 1\n")
    _write(repo / "src" / "pkg" / "deep.py", "DEEP = True\n")
    _write(repo / "src" / "pkg" / "notes.txt", "plain\n")
    _write(repo / "docs" / "guide.md", "guide\n")
    _write(repo / "crlf.txt", b"one\r\ntwo\rthree\n")
    _write(repo / "binary.dat", bytes(range(256)) + b"\xff\xfe\x00tail")
    _write(
        repo / "large.txt",
        "".join(f"line {i:05d} {'x' * 90}\n" for i in range(1, 6001)),
    )
    _write(repo / ".gitignore", "*.log\nbuild/\n# comment\n")
    _write(repo / "debug.log", "ignored\n")
    _write(repo / "build" / "out.txt", "ignored\n")
    _write(repo / ".hidden" / "secret.txt", "hidden\n")
    _write(repo / "node_modules" / "dep" / "index.js", "module.exports = 1\n")
    _write(repo / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(repo / ".code-indexer" / "index" / "marker.bin", b"\x00")
    os.symlink("src/app.py", repo / LINK)
    for path in sorted(repo.rglob("*")):
        os.utime(path, ns=(MTIME_NS, MTIME_NS), follow_symlinks=False)

    snapshot = base / ".versioned" / REPO / SNAPSHOT_LEAF
    _write(snapshot / ".code-indexer" / "metadata.json", "{}")

    clones = {REPO: str(repo)}
    manager = SimpleNamespace(
        get_golden_repo=lambda name: (
            SimpleNamespace(clone_path=clones[name]) if name in clones else None
        )
    )
    monkeypatch.setattr(grm, "get_golden_repo_manager", lambda: manager)
    return base


def _repo(golden: Path) -> Path:
    return golden / REPO


def _snapshot(golden: Path) -> Path:
    return golden / ".versioned" / REPO / SNAPSHOT_LEAF


@pytest.fixture
def service() -> FileListingService:
    return FileListingService()


def _user() -> Any:
    from datetime import datetime

    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="example-user",
        password_hash="unused",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


def _use_backend(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    monkeypatch.setattr(wiring, "server_content_backend", lambda: backend)


def _forbid_local_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every local repository read raise, so delegation must not use one."""

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a local repository read ran under delegation")

    for name in (
        "_collect_files_uncached",
        "_collect_single_level",
        "_load_gitignore_spec",
        "_read_bytes_local",
    ):
        monkeypatch.setattr(FileListingService, name, _boom)


def _qp(**kwargs: Any) -> FileListQueryParams:
    base: Dict[str, Any] = {"page": 1, "limit": 500}
    base.update(kwargs)
    return FileListQueryParams(**base)


def _rows(response: Any) -> List[tuple]:
    return [
        (f.path, f.size_bytes, f.modified_at, f.language, f.is_indexed)
        for f in response.files
    ]


def _split_link(rows: List[tuple]) -> tuple:
    link = [r for r in rows if r[0] == LINK]
    return [r for r in rows if r[0] != LINK], link


QUERIES = [
    {},
    {"path_pattern": "src/**/*"},
    {"path_pattern": "*.md"},
    {"language": "python"},
    {"sort_by": "size"},
    {"sort_by": "modified_at"},
    {"direct_children_of": "src/pkg"},
    {"limit": 3, "page": 2},
]


# --------------------------------------------------------------------------
# list_files / browse_directory (recursive)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize("local_backend", [None, "local"])
def test_listing_matches_local(
    golden: Path,
    service: FileListingService,
    monkeypatch: pytest.MonkeyPatch,
    query: Dict[str, Any],
    local_backend: Optional[str],
) -> None:
    _use_backend(monkeypatch, LocalContentBackend() if local_backend else None)
    local = _rows(service.list_files_by_path(str(_repo(golden)), _qp(**query)))

    fs_mod._collect_cache.clear()
    _use_backend(monkeypatch, DelegatedBackend())
    _forbid_local_reads(monkeypatch)
    delegated = _rows(service.list_files_by_path(str(_snapshot(golden)), _qp(**query)))

    local_rest, local_link = _split_link(local)
    delegated_rest, delegated_link = _split_link(delegated)
    if query.get("sort_by") in ("size", "modified_at"):
        # Ties keep walk order, which os.walk does not define: compare as sets,
        # and check the delegated order is sorted by the key.
        key = 1 if query["sort_by"] == "size" else 2
        assert [r[key] for r in delegated] == sorted(r[key] for r in delegated)
        local_rest, delegated_rest = sorted(local_rest), sorted(delegated_rest)
    assert delegated_rest == local_rest
    assert len(delegated_link) == len(local_link)


def test_the_listing_excludes_and_ignores_like_today(
    golden: Path, service: FileListingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_backend(monkeypatch, DelegatedBackend())
    paths = {
        f.path for f in service.list_files_by_path(str(_snapshot(golden)), _qp()).files
    }
    assert "src/pkg/deep.py" in paths
    assert ".hidden/secret.txt" in paths  # listings never hid dot-files
    assert not {"debug.log", "build/out.txt"} & paths  # root .gitignore
    assert not any(p.startswith((".git/", ".code-indexer/")) for p in paths)


def test_a_symlink_is_listed_unfollowed(
    golden: Path, service: FileListingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented rule: the entry is the link itself, never its target."""
    local = {
        f.path: f.size_bytes
        for f in service.list_files_by_path(str(_repo(golden)), _qp()).files
    }
    fs_mod._collect_cache.clear()
    _use_backend(monkeypatch, DelegatedBackend())
    delegated = {
        f.path: f.size_bytes
        for f in service.list_files_by_path(str(_snapshot(golden)), _qp()).files
    }
    target_size = (_repo(golden) / "src" / "app.py").stat().st_size
    link_size = os.lstat(_repo(golden) / LINK).st_size
    assert local[LINK] == target_size  # followed today
    assert delegated[LINK] == link_size  # the link itself under delegation


def test_the_prune_list_is_sent_on_every_page(
    golden: Path, service: FileListingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = DelegatedBackend(page_size=2)
    _use_backend(monkeypatch, backend)
    service.list_files_by_path(str(_snapshot(golden)), _qp())
    assert len(backend.manifest_calls) > 3
    for call in backend.manifest_calls:
        assert call["root"] == str(_repo(golden))
        assert set(call["prune"]) == FileListingService.ALWAYS_EXCLUDED_DIRS


def test_excluded_directories_stay_out_when_a_backend_does_not_prune(
    golden: Path, service: FileListingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prune list is an optimisation the backend may not honour."""

    class NoPrune(DelegatedBackend):
        def manifest(self, repo_path, *, cursor=None, page_size=1000, prune_dirs=None):
            return super().manifest(repo_path, cursor=cursor, page_size=page_size)

    _use_backend(monkeypatch, NoPrune())
    paths = [
        f.path for f in service.list_files_by_path(str(_snapshot(golden)), _qp()).files
    ]
    assert paths and not any(p.startswith(".code-indexer/") for p in paths)


def test_paging_through_many_pages_is_complete(
    golden: Path, service: FileListingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_backend(monkeypatch, DelegatedBackend())
    whole = _rows(service.list_files_by_path(str(_snapshot(golden)), _qp()))
    fs_mod._collect_cache.clear()
    backend = DelegatedBackend(page_size=1)
    _use_backend(monkeypatch, backend)
    paged = _rows(service.list_files_by_path(str(_snapshot(golden)), _qp()))
    assert paged == whole
    cursors = [c["cursor"] for c in backend.manifest_calls]
    assert cursors[0] is None and all(cursors[1:])


def test_a_stuck_cursor_fails_instead_of_looping(golden: Path) -> None:
    class Stuck(DelegatedBackend):
        def manifest(self, repo_path, *, cursor=None, page_size=1000, prune_dirs=None):
            return ContentPage(entries=(), next_cursor="same")

    source = ContentSeamSource(backend=Stuck(), root=str(_repo(golden)))
    with pytest.raises(RuntimeError, match="did not advance"):
        manifest_entries(source, [".git"])


def test_the_listing_is_cached_for_the_ttl(
    golden: Path, service: FileListingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = DelegatedBackend()
    _use_backend(monkeypatch, backend)
    service.list_files_by_path(str(_snapshot(golden)), _qp())
    calls = (len(backend.manifest_calls), len(backend.fetch_calls))
    service.list_files_by_path(str(_snapshot(golden)), _qp())
    assert (len(backend.manifest_calls), len(backend.fetch_calls)) == calls


def test_indexed_only_still_reads_the_index_on_this_pod(
    golden: Path, service: FileListingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index lives in the snapshot on this pod; files come from the seam."""
    seen: List[str] = []

    def _from_index(_self: Any, repo_path: str) -> Optional[list]:
        seen.append(repo_path)
        return None

    monkeypatch.setattr(FileListingService, "_collect_files_from_index", _from_index)
    _use_backend(monkeypatch, DelegatedBackend())
    result = service.list_files_by_path(str(_snapshot(golden)), _qp(indexed_only=True))
    assert seen == [str(_snapshot(golden))]
    assert result.files  # fell back to the manifest, not the empty snapshot


# --------------------------------------------------------------------------
# browse_directory (one level)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subdir", ["", "src", "src/", "./src", "src/pkg", "docs", "build", ".git", "nope"]
)
def test_one_level_matches_local(
    golden: Path,
    service: FileListingService,
    monkeypatch: pytest.MonkeyPatch,
    subdir: str,
) -> None:
    local = _rows(service.list_directory_level(str(_repo(golden)), subdir, _qp()))
    fs_mod._collect_cache.clear()
    _use_backend(monkeypatch, DelegatedBackend())
    _forbid_local_reads(monkeypatch)
    delegated = _rows(
        service.list_directory_level(str(_snapshot(golden)), subdir, _qp())
    )
    assert _split_link(delegated)[0] == _split_link(local)[0]
    assert [r[0] for r in delegated] == [r[0] for r in local]


@pytest.mark.parametrize("subdir", ["..", "../x", "/etc", "src/../.."])
def test_one_level_refuses_an_escape(
    golden: Path,
    service: FileListingService,
    monkeypatch: pytest.MonkeyPatch,
    subdir: str,
) -> None:
    _use_backend(monkeypatch, DelegatedBackend())
    assert (
        service.list_directory_level(str(_snapshot(golden)), subdir, _qp()).files == []
    )


# --------------------------------------------------------------------------
# get_file_content
# --------------------------------------------------------------------------

READS = [
    ("README.md", {}),
    ("src/pkg/deep.py", {}),
    ("crlf.txt", {}),
    ("binary.dat", {}),
    ("debug.log", {}),  # gitignored files are still readable, as today
    (".git/HEAD", {}),  # so are pruned ones; their time is the epoch placeholder
    ("large.txt", {}),
    ("large.txt", {"offset": 4000, "limit": 50}),
    ("large.txt", {"offset": 5990}),
    ("large.txt", {"skip_truncation": True}),
    ("large.txt", {"skip_truncation": True, "offset": 10, "limit": 7}),
]


@pytest.mark.parametrize("file_path,kwargs", READS)
def test_content_matches_local(
    golden: Path,
    service: FileListingService,
    monkeypatch: pytest.MonkeyPatch,
    file_path: str,
    kwargs: Dict[str, Any],
) -> None:
    local = service.get_file_content_by_path(str(_repo(golden)), file_path, **kwargs)
    _use_backend(monkeypatch, DelegatedBackend())
    _forbid_local_reads(monkeypatch)
    delegated = service.get_file_content_by_path(
        str(_snapshot(golden)), file_path, **kwargs
    )
    if file_path.startswith(".git/"):
        assert (
            delegated["metadata"].pop("modified_at")
            == modified_at_from_ns(None).isoformat()
        )
        local["metadata"].pop("modified_at")
    assert delegated == local


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize(
    "file_path,kwargs",
    [
        ("README.md", {}),
        ("crlf.txt", {}),  # decoded lines lose the \r; the hash must not
        ("binary.dat", {}),  # not UTF-8: decoded with U+FFFD; the hash must not be
        ("large.txt", {"offset": 4000, "limit": 50}),  # a page; the hash is the file's
    ],
)
def test_content_hash_is_the_files_bytes_as_edit_checks_them(
    golden: Path,
    service: FileListingService,
    monkeypatch: pytest.MonkeyPatch,
    delegated: bool,
    file_path: str,
    kwargs: Dict[str, Any],
) -> None:
    """A golden-repo read carries the optimistic-locking hash that edit_file and
    delete_file check (FileCRUDService: SHA-256 of the bytes), so a write-mode
    repo's file can be edited from a read. Mutations: hash the decoded lines
    (crlf/binary differ); hash the returned page (large differs); omit it."""
    from code_indexer.server.services.file_crud_service import FileCRUDService

    data = (_repo(golden) / file_path).read_bytes()
    root = _repo(golden)
    if delegated:
        _use_backend(monkeypatch, DelegatedBackend())
        _forbid_local_reads(monkeypatch)
        root = _snapshot(golden)

    got = service.get_file_content_by_path(str(root), file_path, **kwargs)

    crud = object.__new__(FileCRUDService)
    assert got["metadata"]["content_hash"] == crud._compute_hash(data)


def test_content_reads_one_path_from_the_base_clone(
    golden: Path, service: FileListingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = DelegatedBackend()
    _use_backend(monkeypatch, backend)
    service.get_file_content_by_path(str(_snapshot(golden)), "./src//app.py")
    assert backend.fetch_calls == [["src/app.py"]]
    assert {c["root"] for c in backend.manifest_calls} == {str(_repo(golden))}


@pytest.mark.parametrize(
    "bad", ["/etc/passwd", "../outside.txt", "src/../../outside.txt", "..", None]
)
def test_content_refuses_an_escape(
    golden: Path,
    service: FileListingService,
    monkeypatch: pytest.MonkeyPatch,
    bad: Any,
) -> None:
    backend = DelegatedBackend()
    _use_backend(monkeypatch, backend)
    with pytest.raises(PermissionError):
        service.get_file_content_by_path(str(_snapshot(golden)), bad)
    assert backend.fetch_calls == []


@pytest.mark.parametrize(
    "file_path,message",
    [
        ("missing.py", "File not found: missing.py"),
        ("src", "Not a file: src"),
        ("", "Not a file: "),
        (LINK, f"File not found: {LINK}"),  # the documented symlink rule
    ],
)
def test_content_not_found_matches_todays_errors(
    golden: Path,
    service: FileListingService,
    monkeypatch: pytest.MonkeyPatch,
    file_path: str,
    message: str,
) -> None:
    _use_backend(monkeypatch, DelegatedBackend())
    with pytest.raises(FileNotFoundError) as raised:
        service.get_file_content_by_path(str(_snapshot(golden)), file_path)
    assert str(raised.value) == message


def test_local_errors_are_the_same_messages(
    golden: Path, service: FileListingService
) -> None:
    """Pins the local messages the delegated path mirrors."""
    for file_path, message in [
        ("missing.py", "File not found: missing.py"),
        ("src", "Not a file: src"),
        ("", "Not a file: "),
    ]:
        with pytest.raises(FileNotFoundError) as raised:
            service.get_file_content_by_path(str(_repo(golden)), file_path)
        assert str(raised.value) == message


# --------------------------------------------------------------------------
# directory_tree
# --------------------------------------------------------------------------

TREES: List[Dict[str, Any]] = [
    {},
    {"max_depth": 1},
    {"max_depth": 2, "show_stats": True},
    {"max_depth": 10},
    {"include_hidden": True},
    {"path": "src"},
    {"path": "./src/pkg/"},
    {"path": "README.md"},
    {"include_patterns": ["*.py"]},
    {"exclude_patterns": ["docs"]},
    {"max_files_per_dir": 2},
]


def _tree(node: TreeNode) -> Dict[str, Any]:
    return {
        "name": node.name,
        "path": node.path,
        "dir": node.is_directory,
        "truncated": node.truncated,
        "hidden": node.hidden_count,
        "children": (
            None if node.children is None else [_tree(c) for c in node.children]
        ),
    }


@pytest.mark.parametrize("kwargs", TREES)
def test_tree_matches_local(
    golden: Path, monkeypatch: pytest.MonkeyPatch, kwargs: Dict[str, Any]
) -> None:
    local = DirectoryExplorerService(_repo(golden)).generate_tree(**kwargs)

    def _no_local_walk(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("local tree walk under delegation")

    _use_backend(monkeypatch, DelegatedBackend())
    monkeypatch.setattr(Path, "iterdir", _no_local_walk)
    explorer = directory_explorer_for(str(_snapshot(golden)))
    delegated = explorer.generate_tree(**kwargs)

    assert isinstance(explorer, ManifestDirectoryExplorerService)
    assert delegated.tree_string == local.tree_string
    assert _tree(delegated.root) == _tree(local.root)
    assert (delegated.total_files, delegated.total_directories) == (
        local.total_files,
        local.total_directories,
    )
    assert delegated.max_depth_reached == local.max_depth_reached
    if not kwargs:
        assert LINK in local.tree_string  # a link to a file is a leaf in both


@pytest.mark.parametrize("path", ["nope", "../x", "/etc", "src/../.."])
def test_tree_refuses_a_missing_or_escaping_start(
    golden: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _use_backend(monkeypatch, DelegatedBackend())
    with pytest.raises(ValueError, match="Path does not exist"):
        directory_explorer_for(str(_snapshot(golden))).generate_tree(path=path)


def test_tree_prunes_the_default_excluded_directories(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = DelegatedBackend()
    _use_backend(monkeypatch, backend)
    directory_explorer_for(str(_snapshot(golden)))
    prune = set(backend.manifest_calls[0]["prune"])
    assert {".git", ".code-indexer", "node_modules", "__pycache__"} <= prune
    assert not any("*" in name for name in prune)


def test_a_symlinked_directory_is_a_leaf_under_delegation(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local trees skip a link to a directory; the manifest lists it as a leaf."""
    os.symlink("src", _repo(golden) / "srclink")
    local = DirectoryExplorerService(_repo(golden)).generate_tree()
    _use_backend(monkeypatch, DelegatedBackend())
    delegated = directory_explorer_for(str(_snapshot(golden))).generate_tree()
    assert "srclink" not in local.tree_string
    assert "+-- srclink" in delegated.tree_string or "|-- srclink" in (
        delegated.tree_string
    )


def test_tree_names_the_root_after_the_base_clone(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_backend(monkeypatch, DelegatedBackend())
    result = directory_explorer_for(str(_snapshot(golden))).generate_tree()
    assert result.tree_string.splitlines()[0] == f"{REPO}/"
    assert result.root_path == str(_repo(golden))


def test_manifest_explorer_tolerates_a_file_dir_clash(tmp_path: Path) -> None:
    explorer = ManifestDirectoryExplorerService(
        tmp_path, [("a", False), ("a/b.py", False), ("c/d.py", True)]
    )
    result = explorer.generate_tree()
    assert result.total_files == 2  # "a/b.py" under a FILE named "a" is dropped
    assert result.total_directories == 1


def test_prunable_dir_names_are_the_literal_defaults() -> None:
    names = DirectoryExplorerService.prunable_dir_names()
    assert ".git" in names and "node_modules" in names
    assert "*.pyc" not in names


# --------------------------------------------------------------------------
# root mapping, local no-change, missing registration
# --------------------------------------------------------------------------


@pytest.mark.parametrize("backend", [None, "local"])
def test_a_local_backend_changes_nothing(
    golden: Path,
    service: FileListingService,
    monkeypatch: pytest.MonkeyPatch,
    backend: Optional[str],
) -> None:
    chosen = LocalContentBackend() if backend else None
    _use_backend(monkeypatch, chosen)
    assert content_seam_source(str(_snapshot(golden))) is None
    explorer = directory_explorer_for(str(_snapshot(golden)))
    assert type(explorer) is DirectoryExplorerService
    assert explorer.repo_path == _snapshot(golden)
    # The local path reads the snapshot, which holds no repository files.
    assert service.list_files_by_path(str(_snapshot(golden)), _qp()).files == []


def test_the_seam_root_is_the_base_clone(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = DelegatedBackend()
    _use_backend(monkeypatch, backend)
    source = content_seam_source(str(_snapshot(golden)))
    assert source == ContentSeamSource(backend=backend, root=str(_repo(golden)))
    # A path that is not a snapshot is used as it is.
    other = content_seam_source(str(_repo(golden)))
    assert other is not None and other.root == str(_repo(golden))


def test_an_unregistered_snapshot_falls_back_with_a_warning(
    golden: Path,
    service: FileListingService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gone = golden / ".versioned" / "gone" / SNAPSHOT_LEAF
    _write(gone / ".code-indexer" / "metadata.json", "{}")
    backend = DelegatedBackend()
    _use_backend(monkeypatch, backend)
    with caplog.at_level(logging.WARNING):
        result = service.list_files_by_path(str(gone), _qp())
    assert result.files == []  # today's behaviour: the snapshot path is used
    assert {c["root"] for c in backend.manifest_calls} == {str(gone)}
    assert "no base clone registered for gone" in caplog.text


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def test_fetch_one_returns_the_record_or_an_error(golden: Path) -> None:
    source = ContentSeamSource(backend=DelegatedBackend(), root=str(_repo(golden)))
    assert fetch_one(source, "docs/guide.md").data == b"guide\n"
    assert fetch_one(source, "missing").error is not None

    class Silent(DelegatedBackend):
        def fetch(self, repo_path, paths):
            return iter(())

    silent = ContentSeamSource(backend=Silent(), root=str(_repo(golden)))
    assert fetch_one(silent, "docs/guide.md").error is not None


def test_modified_at_from_ns_matches_stat(golden: Path) -> None:
    path = _repo(golden) / "README.md"
    from datetime import datetime, timezone

    stat = path.stat()
    assert modified_at_from_ns(stat.st_mtime_ns) == datetime.fromtimestamp(
        stat.st_mtime, tz=timezone.utc
    )
    assert modified_at_from_ns(None) == datetime.fromtimestamp(0, tz=timezone.utc)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a/b.py", "a/b.py"),
        ("./a//b.py", "a/b.py"),
        ("a/b/", "a/b"),
        ("", ""),
        (".", ""),
    ],
)
def test_confine_relative_path_normalises(raw: str, expected: str) -> None:
    assert confine_relative_path(raw) == expected


@pytest.mark.parametrize(
    "raw", ["/abs", "..", "a/../b", "../a", None, 5, "x" * 300, "a\x00b", "\ud800"]
)
def test_confine_relative_path_refuses(raw: Any) -> None:
    with pytest.raises(PermissionError):
        confine_relative_path(raw)


def test_the_gitignore_spec_is_parsed_once_for_both_paths() -> None:
    spec = FileListingService._gitignore_spec_from_text("*.log\r\nbuild/\n")
    assert spec.match_file("a.log") and spec.match_file("build/x")
    assert not spec.match_file("a.py")


# --------------------------------------------------------------------------
# the directory_tree front door
# --------------------------------------------------------------------------


def _tree_handler(
    monkeypatch: pytest.MonkeyPatch,
    golden: Path,
    resolved: Optional[str],
    registry_target: Optional[str],
) -> Dict[str, Any]:
    import json

    from code_indexer.server.mcp.handlers import _legacy
    from code_indexer.server.mcp.handlers import files as files_mod

    registry_calls: List[str] = []

    def _registry(alias: str, _user: Any, *_a: Any) -> tuple:
        registry_calls.append(alias)
        if registry_target is None:
            return None, {"content": [{"type": "text", "text": '{"success": false}'}]}
        return registry_target, None

    monkeypatch.setattr(files_mod, "_get_golden_repos_dir", lambda: str(golden))
    monkeypatch.setattr(_legacy, "_resolve_repo_path", lambda *_a: resolved)
    monkeypatch.setattr(files_mod, "_resolve_global_repo_target", _registry)
    monkeypatch.setattr(files_mod, "_get_available_repos", lambda _u: [])
    response = files_mod.handle_directory_tree(
        {"repository_alias": f"{REPO}-global"}, _user()
    )
    data = json.loads(response["content"][0]["text"])
    data["registry_calls"] = registry_calls
    return dict(data)


def test_tree_handler_resolves_from_the_registry_when_the_disk_has_nothing(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_backend(monkeypatch, DelegatedBackend())
    data = _tree_handler(monkeypatch, golden, None, str(_snapshot(golden)))
    assert data["success"] is True
    assert data["registry_calls"] == [f"{REPO}-global"]
    assert "deep.py" in data["tree_string"]


def test_tree_handler_reports_the_registry_error_under_delegation(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_backend(monkeypatch, DelegatedBackend())
    data = _tree_handler(monkeypatch, golden, None, None)
    assert data["success"] is False


@pytest.mark.parametrize("backend", [None, "local"])
def test_tree_handler_keeps_todays_not_found_locally(
    golden: Path, monkeypatch: pytest.MonkeyPatch, backend: Optional[str]
) -> None:
    _use_backend(monkeypatch, LocalContentBackend() if backend else None)
    data = _tree_handler(monkeypatch, golden, None, str(_snapshot(golden)))
    assert data["success"] is False
    assert "not found" in data["error"]
    assert data["registry_calls"] == []


def test_tree_handler_reads_the_manifest_for_a_snapshot_target(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = DelegatedBackend()
    _use_backend(monkeypatch, backend)
    data = _tree_handler(monkeypatch, golden, str(_snapshot(golden)), None)
    assert data["success"] is True
    assert data["tree_string"].startswith(f"{REPO}/")
    assert backend.manifest_calls and data["registry_calls"] == []


# --------------------------------------------------------------------------
# the content backend accessors
# --------------------------------------------------------------------------


def test_server_content_backend_reads_the_running_app_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.utils import registry_factory

    backend = DelegatedBackend()
    state = SimpleNamespace(content_backend=backend)
    monkeypatch.setattr(registry_factory, "_running_server_app_state", lambda: state)
    assert wiring.server_content_backend() is backend
    assert wiring.is_content_seam_delegated() is True
    state.content_backend = LocalContentBackend()
    assert wiring.is_content_seam_delegated() is False
    monkeypatch.setattr(registry_factory, "_running_server_app_state", lambda: None)
    assert wiring.server_content_backend() is None
    assert wiring.is_content_seam_delegated() is False


def test_golden_repo_content_root_maps_only_registered_snapshots(
    golden: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = wiring.golden_repo_content_root
    assert root(_snapshot(golden), consumer="X") == _repo(golden)
    assert root(str(_repo(golden)), consumer="X") == _repo(golden)
    gone = golden / ".versioned" / "gone" / SNAPSHOT_LEAF
    with caplog.at_level(logging.WARNING):
        assert root(gone, consumer="Some tool") == gone
    assert "Some tool: no base clone registered for gone" in caplog.text


def test_the_registry_failing_is_no_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_indexer.server.repositories import golden_repo_manager as grm

    def _broken() -> Any:
        raise RuntimeError("no registry in this process")

    monkeypatch.setattr(grm, "get_golden_repo_manager", _broken)
    assert wiring._golden_repo_clone_path(REPO) is None


# --------------------------------------------------------------------------
# the list_files / browse_directory / get_file_content front doors
# --------------------------------------------------------------------------


@pytest.fixture
def front_door(golden: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The MCP file handlers with the alias resolving to the snapshot."""
    from code_indexer.server.mcp.handlers import _utils
    from code_indexer.server.mcp.handlers import files as files_mod

    monkeypatch.setattr(
        _utils, "app_module", SimpleNamespace(file_service=FileListingService())
    )
    monkeypatch.setattr(
        files_mod,
        "_resolve_global_repo_target",
        lambda *_a: (str(_snapshot(golden)), None),
    )
    _use_backend(monkeypatch, DelegatedBackend())
    _forbid_local_reads(monkeypatch)
    return files_mod


def _payload(response: Dict[str, Any]) -> Dict[str, Any]:
    import json

    return dict(json.loads(response["content"][0]["text"]))


def test_list_files_front_door_reads_the_seam(front_door: Any) -> None:
    user = _user()
    data = _payload(front_door.list_files({"repository_alias": f"{REPO}-global"}, user))
    assert data["success"] is True
    assert "src/pkg/deep.py" in {f["path"] for f in data["files"]}


@pytest.mark.parametrize("recursive", [True, False])
def test_browse_directory_front_door_reads_the_seam(
    front_door: Any, recursive: bool
) -> None:
    user = _user()
    data = _payload(
        front_door.browse_directory(
            {
                "repository_alias": f"{REPO}-global",
                "path": "src/pkg",
                "recursive": recursive,
            },
            user,
        )
    )
    assert data["success"] is True
    paths = {f["path"] for f in data["structure"]["files"]}
    assert paths == {"src/pkg/deep.py", "src/pkg/notes.txt"}


def test_get_file_content_front_door_reads_the_seam(front_door: Any) -> None:
    user = _user()
    data = _payload(
        front_door.get_file_content(
            {"repository_alias": f"{REPO}-global", "file_path": "src/app.py"}, user
        )
    )
    assert data["success"] is True
    assert data["file_content"][0]["text"] == "def main():\n    return 1\n"
    refused = _payload(
        front_door.get_file_content(
            {"repository_alias": f"{REPO}-global", "file_path": "../x"}, user
        )
    )
    assert refused["success"] is False
