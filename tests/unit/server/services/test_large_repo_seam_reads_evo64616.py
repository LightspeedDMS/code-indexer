"""Large-repository reads through the content seam (issue-64616).

On a 32k-file, 2 GB golden repository every whole-repository manifest walk
cost workspace-mcp 40-60 s (it hashes every byte). These tests pin the rules
that keep the file tools off that walk:

* a single-file read is ONE fetch -- the fetch's own error says "not a file" /
  "not found", and the modified time comes from the frame header (or one
  shallow listing of the file's directory) -- never the manifest;
* a one-level browse is ONE scoped listing (``prefix`` + ``max_depth: 1``,
  ``hash: false``) when the backend honours the manifest options;
* every other listing shares ONE cached, unhashed manifest per published
  golden version, whatever the caller's own prune names;
* a backend that predates the options keeps working, and one that advertises
  them but ignores them is caught by its answer.

The large fakes refuse the whole-repository walk outright, so any regression
back to it fails loudly rather than just slowly.
"""

from __future__ import annotations

import dataclasses
import errno
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

import pytest

from code_indexer.global_repos import refresh_scheduler
from code_indexer.server.models.api_models import FileListQueryParams
from code_indexer.server.services import activated_copy_content as acc
from code_indexer.server.services import file_service as fs_mod
from code_indexer.server.services.config_service import reset_config_service
from code_indexer.server.services.file_service import (
    ContentSeamSource,
    FileListingService,
    manifest_entries,
    modified_at_from_ns,
    scoped_manifest_entries,
)
from code_indexer.server.startup import content_backend_wiring as wiring
from code_indexer.server.storage.shared import content_backend as cb
from code_indexer.server.storage.shared.content_backend import (
    MANIFEST_OPTIONS,
    ContentEntry,
    ContentPage,
    FetchedFile,
    LocalContentBackend,
    entry_in_scope,
    query_manifest_pages,
)

REPO = "example-repo"
SNAPSHOT = "v_1760000000"
MTIME_NS = 1_700_000_000_123_456_789
#: Files in the synthetic large repository: big enough that a whole walk is
#: obviously not "one directory".
LARGE_FILE_COUNT = 40_000


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class LargeBackend:
    """A NON-local backend over a synthetic 40k-file repository.

    ``src/mod<i>/file<j>.java`` plus a few root files. Nothing is on disk.
    ``forbid_full`` makes an unscoped manifest request an assertion failure:
    that walk is the minute-long cost the file tools must not pay.
    """

    def __init__(
        self,
        *,
        options: Sequence[str] = (),
        mtime_header: bool = False,
        forbid_full: bool = True,
        honour_scope: bool = True,
    ) -> None:
        if options:
            self.manifest_options = frozenset(options)
        self._mtime_header = mtime_header
        self._forbid_full = forbid_full
        self._honour_scope = honour_scope
        self.manifest_calls: List[Dict[str, Any]] = []
        self.fetch_calls: List[List[str]] = []
        paths = ["README.md", "pom.xml"]
        per_dir = 100
        for i in range(LARGE_FILE_COUNT // per_dir):
            paths += [f"src/mod{i:04d}/File{j:03d}.java" for j in range(per_dir)]
        self._paths = sorted(paths)
        self._dirs = {
            "/".join(p.split("/")[:k])
            for p in self._paths
            for k in range(1, p.count("/") + 1)
        }

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
        **options: Any,
    ) -> ContentPage:
        self.manifest_calls.append(
            {"cursor": cursor, "prune": sorted(prune_dirs or ()), **options}
        )
        prefix = options.get("prefix") if self._honour_scope else None
        max_depth = options.get("max_depth") if self._honour_scope else None
        if self._forbid_full and not prefix and max_depth is None:
            raise AssertionError("whole-repository manifest requested")
        hashed = options.get("hash", True)
        selected = [
            p
            for p in self._paths
            if entry_in_scope(p, prefix or "", max_depth)
            and (cursor is None or p > cursor)
        ][:page_size]
        entries = tuple(
            ContentEntry(
                path=p,
                size=len(p),
                blob_sha=("0" * 40) if hashed else None,
                is_symlink=False,
                mtime_ns=MTIME_NS,
            )
            for p in selected
        )
        more = len(selected) == page_size
        return ContentPage(entries=entries, next_cursor=selected[-1] if more else None)

    def fetch(
        self, repo_path: Union[str, Path], paths: Sequence[str]
    ) -> Iterator[FetchedFile]:
        self.fetch_calls.append(list(paths))
        for rel in paths:
            if rel in self._dirs:
                yield FetchedFile(path=rel, blob_sha=None, data=b"", error="EISDIR")
            elif rel == "huge.bin":
                yield FetchedFile(path=rel, blob_sha=None, data=b"", error="EFBIG")
            elif rel not in self._paths:
                yield FetchedFile(path=rel, blob_sha=None, data=b"", error="ENOENT")
            else:
                yield FetchedFile(
                    path=rel,
                    blob_sha=None,
                    data=f"// {rel}\n".encode(),
                    mtime_ns=MTIME_NS if self._mtime_header else None,
                )


@pytest.fixture(autouse=True)
def _isolated(tmp_path_factory, monkeypatch):
    monkeypatch.setenv(
        "CIDX_SERVER_DATA_DIR", str(tmp_path_factory.mktemp("server-data"))
    )
    reset_config_service()
    fs_mod._collect_cache.clear()
    fs_mod.clear_manifest_cache()
    cb._IGNORES_SCOPE_OPTIONS.clear()
    yield
    fs_mod._collect_cache.clear()
    fs_mod.clear_manifest_cache()
    cb._IGNORES_SCOPE_OPTIONS.clear()
    reset_config_service()


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A registered golden repo whose snapshot holds index state only."""
    from code_indexer.server.repositories import golden_repo_manager as grm

    base = tmp_path / "golden-repos"
    (base / REPO).mkdir(parents=True)
    (base / ".versioned" / REPO / SNAPSHOT / ".code-indexer").mkdir(parents=True)
    clones = {REPO: str(base / REPO)}
    manager = SimpleNamespace(
        golden_repos_dir=str(base),
        get_golden_repo=lambda name: (
            SimpleNamespace(clone_path=clones[name], repo_url="https://example.com/r")
            if name in clones
            else None
        ),
    )
    monkeypatch.setattr(grm, "get_golden_repo_manager", lambda: manager)
    return base


def _snapshot(golden: Path) -> str:
    return str(golden / ".versioned" / REPO / SNAPSHOT)


def _use(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    monkeypatch.setattr(wiring, "server_content_backend", lambda: backend)


def _qp() -> FileListQueryParams:
    params: Dict[str, Any] = {"page": 1, "limit": 500}
    return FileListQueryParams(**params)


SCOPING = sorted(MANIFEST_OPTIONS)


# --------------------------------------------------------------------------
# single-file reads: one fetch, never the manifest
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "options,header",
    [((), False), ((), True), (SCOPING, False), (SCOPING, True)],
    ids=["legacy", "header-only", "scoping-only", "modern"],
)
def test_reading_one_file_never_requests_the_whole_manifest(
    golden: Path, monkeypatch: pytest.MonkeyPatch, options: Any, header: bool
) -> None:
    backend = LargeBackend(options=options, mtime_header=header)
    _use(monkeypatch, backend)
    rel = "src/mod0123/File042.java"
    got = FileListingService().get_file_content_by_path(_snapshot(golden), rel)
    assert got["content"] == f"// {rel}\n"
    assert backend.fetch_calls == [[rel]]
    known = header or bool(options)
    expected = modified_at_from_ns(MTIME_NS if known else None)
    assert got["metadata"]["modified_at"] == expected.isoformat()
    if header:
        assert backend.manifest_calls == []  # the frame header sufficed
    elif options:
        # One shallow, unhashed listing of the file's own directory.
        assert [
            (c.get("prefix"), c.get("max_depth"), c.get("hash"))
            for c in backend.manifest_calls
        ] == [("src/mod0123", 1, False)]
    else:
        assert backend.manifest_calls == []


@pytest.mark.parametrize(
    "rel,error,message",
    [
        ("src/mod0001", FileNotFoundError, "Not a file: src/mod0001"),
        ("src/nope.java", FileNotFoundError, "File not found: src/nope.java"),
        ("huge.bin", OSError, "Could not read huge.bin: EFBIG"),
    ],
)
def test_the_fetch_error_says_what_is_at_the_path(
    golden: Path,
    monkeypatch: pytest.MonkeyPatch,
    rel: str,
    error: type,
    message: str,
) -> None:
    backend = LargeBackend()
    _use(monkeypatch, backend)
    with pytest.raises(error) as raised:
        FileListingService().get_file_content_by_path(_snapshot(golden), rel)
    assert str(raised.value) == message
    if error is OSError:
        assert not isinstance(raised.value, FileNotFoundError)
    assert backend.manifest_calls == []


def test_an_activated_copy_file_read_is_one_fetch(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LargeBackend()
    source = ContentSeamSource(backend=backend, root="/copies/u/example")
    assert acc.read_copy_file(source, "README.md") == b"// README.md\n"
    with pytest.raises(acc.CopyPathError) as missing:
        acc.read_copy_file(source, "nope.txt")
    with pytest.raises(acc.CopyPathError) as directory:
        acc.read_copy_file(source, "src")
    with pytest.raises(acc.CopyContentUnavailableError):
        acc.read_copy_file(source, "huge.bin")
    with pytest.raises(acc.CopyPathError) as index_state:
        acc.read_copy_file(source, ".code-indexer/index/meta.json")
    assert (missing.value.kind, directory.value.kind) == ("missing", "not_file")
    assert index_state.value.kind == "missing"
    assert backend.manifest_calls == []
    assert [".code-indexer/index/meta.json"] not in backend.fetch_calls


def test_an_activated_subdirectory_listing_is_scoped(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LargeBackend(options=SCOPING)
    source = ContentSeamSource(backend=backend, root="/copies/u/example")
    listed = acc.list_copy_directory(
        source, "src/mod0007", recursive=False, max_depth=None, limit=1000
    )
    assert len(listed["files"]) == 100
    assert {c.get("prefix") for c in backend.manifest_calls} == {"src/mod0007"}
    with pytest.raises(acc.CopyPathError) as not_dir:
        acc.list_copy_directory(
            source, "README.md", recursive=False, max_depth=None, limit=10
        )
    assert not_dir.value.kind == "not_dir"


# --------------------------------------------------------------------------
# listings
# --------------------------------------------------------------------------


def test_a_one_level_browse_is_one_shallow_listing(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LargeBackend(options=SCOPING)
    _use(monkeypatch, backend)
    service = FileListingService()
    response = service.list_directory_level(_snapshot(golden), "src/mod0042", _qp())
    assert len(response.files) == 100
    assert all(f.path.startswith("src/mod0042/") for f in response.files)
    calls = [c for c in backend.manifest_calls]
    assert [(c["prefix"], c["max_depth"], c["hash"]) for c in calls] == [
        ("src/mod0042", 1, False)
    ]
    # The root .gitignore is one fetch, never a listing.
    assert backend.fetch_calls == [[".gitignore"]]


def test_a_root_browse_is_one_shallow_listing(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LargeBackend(options=SCOPING)
    _use(monkeypatch, backend)
    response = FileListingService().list_directory_level(_snapshot(golden), "", _qp())
    assert sorted(f.path for f in response.files) == ["README.md", "pom.xml"]
    assert [(c.get("prefix"), c["max_depth"]) for c in backend.manifest_calls] == [
        (None, 1)
    ]


def test_a_legacy_backend_browses_from_one_cached_walk(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LargeBackend(forbid_full=False)
    _use(monkeypatch, backend)
    service = FileListingService()
    first = service.list_directory_level(_snapshot(golden), "src/mod0001", _qp())
    walks = len(backend.manifest_calls)
    second = service.list_directory_level(_snapshot(golden), "src/mod0002", _qp())
    assert len(first.files) == len(second.files) == 100
    assert len(backend.manifest_calls) == walks  # served from the cache
    assert all("prefix" not in c for c in backend.manifest_calls)


def test_a_backend_ignoring_the_scope_is_caught_and_still_correct(
    golden: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    backend = LargeBackend(options=SCOPING, honour_scope=False, forbid_full=False)
    _use(monkeypatch, backend)
    service = FileListingService()
    response = service.list_directory_level(_snapshot(golden), "src/mod0003", _qp())
    assert len(response.files) == 100
    assert all(f.path.startswith("src/mod0003/") for f in response.files)
    assert "prefix" in backend.manifest_calls[0]
    assert "did not honour them" in caplog.text
    assert cb.manifest_options_supported(backend) == {"hash"}
    before = len(backend.manifest_calls)
    service.list_directory_level(_snapshot(golden), "src/mod0004", _qp())
    assert all("prefix" not in c for c in backend.manifest_calls[before:])


def test_every_prune_set_shares_one_walk(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four consumers, four prune sets, ONE walk of the repository."""
    backend = LargeBackend(options=SCOPING, forbid_full=False)
    _use(monkeypatch, backend)
    source = fs_mod.content_seam_source(_snapshot(golden))
    assert source is not None
    for prune in [(), (".git", ".code-indexer"), ("node_modules",), ("mod0001",)]:
        manifest_entries(source, prune)
    starts = [c for c in backend.manifest_calls if c["cursor"] is None]
    assert len(starts) == 1
    assert starts[0]["prune"] == sorted(fs_mod.MANIFEST_BASE_PRUNE)
    assert starts[0]["hash"] is False
    pruned = manifest_entries(source, ("mod0001",))
    assert not any(e.path.startswith("src/mod0001/") for e in pruned)


def test_the_manifest_is_cached_per_published_version_not_per_ttl(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LargeBackend(forbid_full=False)
    _use(monkeypatch, backend)
    monkeypatch.setattr(fs_mod, "_COLLECT_CACHE_TTL_SECONDS", 0.0)
    source = fs_mod.content_seam_source(_snapshot(golden))
    assert source is not None and source.version == _snapshot(golden)
    manifest_entries(source, ())
    walks = len(backend.manifest_calls)
    time.sleep(0.01)
    manifest_entries(source, ())  # far past the TTL: still the same version
    assert len(backend.manifest_calls) == walks
    republished = ContentSeamSource(
        backend=backend, root=source.root, version=source.version + "-next"
    )
    manifest_entries(republished, ())  # a new publish is a new walk
    assert len(backend.manifest_calls) == 2 * walks


def test_a_mutable_source_keeps_the_short_ttl(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LargeBackend(forbid_full=False)
    monkeypatch.setattr(fs_mod, "_COLLECT_CACHE_TTL_SECONDS", 0.0)
    source = ContentSeamSource(backend=backend, root="/copies/u/example")
    manifest_entries(source, ())
    walks = len(backend.manifest_calls)
    time.sleep(0.01)
    manifest_entries(source, ())
    assert len(backend.manifest_calls) == 2 * walks


def test_concurrent_cold_readers_share_one_walk(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowBackend(LargeBackend):
        def manifest(self, *args: Any, **kwargs: Any) -> ContentPage:
            time.sleep(0.002)
            return super().manifest(*args, **kwargs)

    backend = SlowBackend(forbid_full=False)
    source = ContentSeamSource(backend=backend, root="/r", version="/v/1")
    threads = [
        threading.Thread(target=manifest_entries, args=(source, ())) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([c for c in backend.manifest_calls if c["cursor"] is None]) == 1


def test_concurrent_cold_scoped_readers_share_one_listing(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowBackend(LargeBackend):
        def manifest(self, *args: Any, **kwargs: Any) -> ContentPage:
            time.sleep(0.02)
            return super().manifest(*args, **kwargs)

    backend = SlowBackend(options=SCOPING)
    source = ContentSeamSource(backend=backend, root="/r", version="/v/1")
    results: List[int] = []

    def read() -> None:
        results.append(len(scoped_manifest_entries(source, "src/mod0001", max_depth=1)))

    threads = [threading.Thread(target=read) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [100] * 6
    assert len(backend.manifest_calls) == 1  # one backend listing for all six
    assert fs_mod._manifest_build_locks == {}  # the build lock is not kept


def test_browsing_many_directories_never_evicts_a_whole_manifest(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LargeBackend(options=SCOPING, forbid_full=False)
    whole = ContentSeamSource(backend=backend, root="/r", version="/v/1")
    manifest_entries(whole, ())
    walks = len(backend.manifest_calls)
    other = ContentSeamSource(backend=backend, root="/r2", version="/v/2")
    for i in range(fs_mod._MANIFEST_CACHE_MAX * 4):
        scoped_manifest_entries(other, f"src/mod{i:04d}", max_depth=1)
    manifest_entries(whole, ())
    assert len(backend.manifest_calls) == walks + fs_mod._MANIFEST_CACHE_MAX * 4


def test_scoped_listings_are_bounded_by_the_entries_they_hold(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs_mod, "_SCOPED_MANIFEST_MAX_ENTRIES", 250)
    backend = LargeBackend(options=SCOPING)
    source = ContentSeamSource(backend=backend, root="/r", version="/v/1")
    for i in range(4):  # 100 entries each
        scoped_manifest_entries(source, f"src/mod{i:04d}", max_depth=1)
    held = fs_mod._scoped_manifest_cache
    assert sum(len(v[1]) for v in held.values()) <= 250
    assert len(held) == 2  # the two least recently used were evicted
    before = len(backend.manifest_calls)
    scoped_manifest_entries(source, "src/mod0003", max_depth=1)  # still held
    assert len(backend.manifest_calls) == before
    scoped_manifest_entries(source, "src/mod0000", max_depth=1)  # evicted
    assert len(backend.manifest_calls) == before + 1


def test_scoped_entries_reuse_a_held_manifest(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LargeBackend(options=SCOPING, forbid_full=False)
    source = ContentSeamSource(backend=backend, root="/r", version="/v/1")
    manifest_entries(source, ())
    walks = len(backend.manifest_calls)
    got = scoped_manifest_entries(source, "src/mod0009", max_depth=1)
    assert len(got) == 100
    assert len(backend.manifest_calls) == walks


def test_repository_stats_prune_and_share_the_walk(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.repositories.repository_listing_manager import (
        RepositoryListingManager,
    )

    backend = LargeBackend(options=SCOPING, forbid_full=False)
    source = ContentSeamSource(backend=backend, root="/r", version="/v/1")
    stats = RepositoryListingManager._manifest_statistics(
        source, {"alias": REPO, "created_at": "2026-01-01T00:00:00+00:00"}
    )
    assert stats["file_count"] == LARGE_FILE_COUNT + 2
    assert backend.manifest_calls[0]["prune"] == sorted(fs_mod.MANIFEST_BASE_PRUNE)
    walks = len(backend.manifest_calls)
    manifest_entries(source, (".git",))
    assert len(backend.manifest_calls) == walks


def test_the_extension_probe_asks_unhashed(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LargeBackend(options=SCOPING, forbid_full=False)
    assert refresh_scheduler._manifest_has_files_with_extensions(
        backend, "/r", {"java"}, {"node_modules"}
    )
    assert backend.manifest_calls[0]["hash"] is False
    legacy = LargeBackend(forbid_full=False)
    assert not refresh_scheduler._manifest_has_files_with_extensions(
        legacy, "/r", {"cpp"}, set()
    )
    assert all("hash" not in c for c in legacy.manifest_calls)


def test_a_tree_from_a_path_lists_only_that_subtree(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LargeBackend(options=SCOPING)
    _use(monkeypatch, backend)
    result = fs_mod.directory_explorer_for(
        _snapshot(golden), path="src/mod0005"
    ).generate_tree(path="src/mod0005", max_depth=1, max_files_per_dir=1000)
    assert result.total_files == 100
    assert {c.get("prefix") for c in backend.manifest_calls} == {"src/mod0005"}


# --------------------------------------------------------------------------
# the delegated plugin's own API (cidx-content-backend 0.11.0)
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _PluginEntry:
    path: str
    size: int
    blob_sha: Optional[str]
    is_symlink: bool
    mtime_ns: Optional[int] = None
    is_dir: bool = False
    error: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class _PluginPage:
    entries: Tuple[_PluginEntry, ...]
    next_cursor: Optional[str]
    options: Any = None
    emulated: bool = False


class PluginShapedBackend:
    """``WorkspaceMcpContentBackend``'s 0.11.0 surface: keyword options,
    ``manifest_options_supported``, directory entries at the depth limit and
    ``emulated`` pages from a server that predates the options."""

    def __init__(self, *, server_has_options: bool) -> None:
        self.manifest_options_supported: Optional[bool] = None
        self._server_has_options = server_has_options
        self._inner = LargeBackend(options=SCOPING, forbid_full=False)
        self.manifest_calls: List[Dict[str, Any]] = []

    @property
    def is_local(self) -> bool:
        return False

    def manifest(
        self,
        repo: Union[str, Path],
        *,
        cursor: Optional[str] = None,
        page_size: int = 5000,
        prune_dirs: Optional[Sequence[str]] = None,
        hash: bool = True,
        prefix: str = "",
        max_depth: Optional[int] = None,
    ) -> _PluginPage:
        self.manifest_calls.append(
            {"prefix": prefix, "max_depth": max_depth, "hash": hash}
        )
        page = self._inner.manifest(
            repo,
            cursor=cursor,
            page_size=page_size,
            prune_dirs=prune_dirs,
            hash=hash,
            prefix=prefix,
            max_depth=max_depth,
        )
        entries = tuple(
            _PluginEntry(e.path, e.size, e.blob_sha, False, e.mtime_ns)
            for e in page.entries
        )
        if max_depth is not None:
            here = f"{prefix}/" if prefix else ""
            entries += (_PluginEntry(f"{here}zz-subdir", 0, None, False, None, True),)
        emulated = bool(prefix or max_depth) and not self._server_has_options
        self.manifest_options_supported = not emulated
        return _PluginPage(entries, page.next_cursor, None, emulated)

    def fetch(
        self, repo: Union[str, Path], paths: Sequence[str]
    ) -> Iterator[FetchedFile]:
        return self._inner.fetch(repo, paths)


def test_the_plugin_api_is_detected_and_directory_entries_dropped(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = PluginShapedBackend(server_has_options=True)
    _use(monkeypatch, backend)
    assert cb.manifest_options_supported(backend) == MANIFEST_OPTIONS
    response = FileListingService().list_directory_level(
        _snapshot(golden), "src/mod0010", _qp()
    )
    assert len(response.files) == 100  # the is_dir entry is not a file
    assert backend.manifest_calls == [
        {"prefix": "src/mod0010", "max_depth": 1, "hash": False}
    ]


def test_an_emulated_plugin_page_falls_back_to_the_cached_manifest(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = PluginShapedBackend(server_has_options=False)
    _use(monkeypatch, backend)
    service = FileListingService()
    first = service.list_directory_level(_snapshot(golden), "src/mod0011", _qp())
    assert len(first.files) == 100
    assert cb.manifest_options_supported(backend) == {"hash"}
    walks = len(backend.manifest_calls)
    second = service.list_directory_level(_snapshot(golden), "src/mod0012", _qp())
    assert len(second.files) == 100
    assert len(backend.manifest_calls) == walks  # the cached whole manifest


def test_a_plugin_saying_the_server_lacks_options_is_sent_only_hash() -> None:
    backend = PluginShapedBackend(server_has_options=False)
    backend.manifest_options_supported = False
    assert cb.manifest_options_supported(backend) == {"hash"}


# --------------------------------------------------------------------------
# the option semantics, on the reference (local) backend
# --------------------------------------------------------------------------


def _tree(tmp_path: Path) -> Path:
    for rel in [
        "a.txt",
        "b/c.txt",
        "b/d/e.txt",
        "b/d/f/g.txt",
        "b/node_modules/x.js",
        "z.txt",
    ]:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rel)
    os.symlink("b", tmp_path / "blink")
    return tmp_path


def _paths(pages: Sequence[ContentPage]) -> List[str]:
    return [e.path for p in pages for e in p.entries]


@pytest.mark.parametrize(
    "prefix,max_depth,expected",
    [
        ("", None, ["a.txt", "b/c.txt", "b/d/e.txt", "b/d/f/g.txt", "blink", "z.txt"]),
        ("", 1, ["a.txt", "blink", "z.txt"]),
        ("b", None, ["b/c.txt", "b/d/e.txt", "b/d/f/g.txt"]),
        ("b", 1, ["b/c.txt"]),
        ("b/d", 2, ["b/d/e.txt", "b/d/f/g.txt"]),
        ("b/missing", None, []),
        ("a.txt", None, []),
        ("blink", None, []),  # never through a symlink
        ("b/node_modules", None, []),  # never inside a pruned directory
    ],
)
def test_local_backend_honours_prefix_and_depth(
    tmp_path: Path, prefix: str, max_depth: Optional[int], expected: List[str]
) -> None:
    root = _tree(tmp_path)
    pages = list(
        query_manifest_pages(
            LocalContentBackend(),
            root,
            prune_dirs=("node_modules",),
            prefix=prefix,
            max_depth=max_depth,
            page_size=1,
        )
    )
    assert _paths(pages) == expected


def test_local_backend_skips_the_hash_on_request(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    page = LocalContentBackend().manifest(root, hash=False)
    assert page.entries and all(e.blob_sha is None for e in page.entries)
    assert all(e.mtime_ns is not None for e in page.entries)


@pytest.mark.parametrize("bad", ["../x", "/etc", "a/../b", "./b"])
def test_a_prefix_must_be_a_plain_relative_path(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        LocalContentBackend().manifest(_tree(tmp_path), prefix=bad)


def test_a_cursor_outside_the_prefix_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LocalContentBackend().manifest(_tree(tmp_path), prefix="b", cursor="a.txt")


def test_a_backend_without_options_is_sent_none(tmp_path: Path) -> None:
    calls: List[Dict[str, Any]] = []

    class Old:
        is_local = False

        def manifest(self, root: Any, **kwargs: Any) -> ContentPage:
            calls.append(kwargs)
            return LocalContentBackend().manifest(root, **kwargs)

    pages = list(
        query_manifest_pages(
            cast(Any, Old()),
            _tree(tmp_path),
            prune_dirs=("node_modules",),
            prefix="b",
            want_hash=False,
        )
    )
    assert _paths(pages) == ["b/c.txt", "b/d/e.txt", "b/d/f/g.txt"]
    assert set(calls[0]) == {"cursor", "page_size", "prune_dirs"}


# --------------------------------------------------------------------------
# the published version a listing is cached under
# --------------------------------------------------------------------------


def test_the_version_of_a_snapshot_is_the_snapshot(golden: Path) -> None:
    assert wiring.golden_content_version(_snapshot(golden)) == _snapshot(golden)


def test_the_version_of_a_base_clone_is_its_alias_target(
    golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.global_repos import alias_manager

    targets: Dict[str, Optional[str]] = {f"{REPO}-global": _snapshot(golden)}
    monkeypatch.setattr(
        alias_manager.AliasManager,
        "read_alias",
        lambda self, name: targets.get(name),
    )
    assert wiring.golden_content_version(str(golden / REPO)) == _snapshot(golden)
    # A write session points the alias at the live clone: no stable version.
    targets[f"{REPO}-global"] = str(golden / REPO)
    assert wiring.golden_content_version(str(golden / REPO)) is None


@pytest.mark.parametrize("path", ["/copies/u/example", "/elsewhere/example-repo", ""])
def test_anything_else_has_no_version(golden: Path, path: str) -> None:
    assert wiring.golden_content_version(path) is None


def test_fetched_mtime_is_read_defensively() -> None:
    assert cb.fetched_mtime_ns(SimpleNamespace(mtime_ns=5)) == 5
    assert cb.fetched_mtime_ns(SimpleNamespace()) is None
    assert cb.fetched_mtime_ns(SimpleNamespace(mtime_ns="5")) is None
    assert cb.fetched_mtime_ns(SimpleNamespace(mtime_ns=True)) is None


def test_directory_and_not_found_errors_are_recognised() -> None:
    assert cb.is_directory_fetch_error("EISDIR")
    assert cb.is_directory_fetch_error(os.strerror(errno.EISDIR))
    assert cb.is_not_found_fetch_error("ENOENT")
    assert cb.is_not_found_fetch_error("EACCES")
    assert not cb.is_not_found_fetch_error("EIO")
