"""issue-64616: alias / write-mode FILES go through the content seam when delegated.

The pointers live in the registry, but their file representation is still read
by the startup import, by the read-through on a registry miss before that
import finishes, and by the ``metadata_read_source=files`` rollback (which
writes them too). ``MetadataFiles`` did all of it on this pod's disk -- which,
on a pod without the golden-repos volume, holds nothing: the import "finished"
with nothing, a read-through answered "no such alias". Under
``content_backend='delegated'`` the installed component now uses
``SeamMetadataFiles``: the same paths, read through the content backend and
written through the content writer.

The fakes stand in for workspace-mcp at the seam's own interface (manifest /
fetch / put / delete over absolute paths); a decoy file on the local disk with
DIFFERENT content proves the local disk is never read.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, cast

import pytest

import code_indexer.server.startup.content_backend_wiring as content_wiring
import code_indexer.server.startup.content_writer_wiring as writer_wiring
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.control_plane_metadata import (
    KIND_ALIAS,
    KIND_WRITE_MODE,
    ControlPlaneMetadata,
    MetadataFiles,
    MetadataSettings,
    install_control_plane_metadata,
    installed_control_plane_metadata,
    uninstall_control_plane_metadata,
)
from code_indexer.server.services.control_plane_metadata_seam import (
    MetadataFilesUnavailableError,
    SeamMetadataFiles,
)
from code_indexer.server.startup.control_plane_metadata_wiring import (
    install_registry_metadata,
)
from code_indexer.server.storage.shared.content_backend import (
    ContentEntry,
    ContentPage,
    FetchedFile,
    is_absent_root_error,
)

ALIAS = "example-repo-global"


def _doc(target: str) -> Dict[str, Any]:
    return {
        "target_path": target,
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_refresh": "2026-01-01T00:00:00+00:00",
        "repo_name": "example-repo",
    }


class _HttpError(RuntimeError):
    """Shaped like the delegated plugin's ``ContentMcpHttpError``: the status
    and the response body, as workspace-mcp's NestJS error answer."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.detail = json.dumps(
            {"statusCode": status, "message": message, "error": "Bad Request"}
        ).encode()
        super().__init__(f"/content/manifest returned {status}: {message}")


class _WorkspaceMcp:
    """Absolute path -> bytes, served as a content backend and a writer.

    Like the real server, a manifest of a directory that does not exist is a
    400 ``source_path does not exist`` (``resolveExistingUnderRoot``), not an
    empty page. A directory exists once a file was written under it, or when
    listed in ``dirs`` (created, then emptied).
    """

    is_local = False

    def __init__(self) -> None:
        self.files: Dict[str, bytes] = {}
        self.dirs: Set[str] = set()
        self.puts: List[str] = []
        self.deletes: List[str] = []
        self.manifest_error: Optional[Exception] = None

    def _exists(self, root: PurePosixPath) -> bool:
        if str(root) in self.dirs:
            return True
        return any(root in PurePosixPath(path).parents for path in self.files)

    # content backend
    def manifest(
        self,
        repo_path: Any,
        *,
        cursor: Optional[str] = None,
        page_size: int = 1000,
        prune_dirs: Optional[Sequence[str]] = None,
    ) -> ContentPage:
        root = PurePosixPath(str(repo_path))
        if self.manifest_error is not None:
            raise self.manifest_error
        if not self._exists(root):
            raise _HttpError(400, f"source_path does not exist: {root}")
        entries = []
        for path, data in sorted(self.files.items()):
            try:
                rel = PurePosixPath(path).relative_to(root)
            except ValueError:
                continue
            if any(part in (prune_dirs or ()) for part in rel.parts[:-1]):
                continue
            entries.append(ContentEntry(str(rel), len(data), "0" * 40, False))
        return ContentPage(tuple(entries), None)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        for rel in paths:
            data = self.files.get(str(PurePosixPath(str(repo_path)) / rel))
            if data is None:
                yield FetchedFile(rel, None, b"", error="ENOENT")
            else:
                yield FetchedFile(rel, "0" * 40, data)

    # content writer
    def put(self, path: str, data: bytes, **_kw: Any) -> None:
        self.puts.append(path)
        self.files[path] = data

    def delete(self, path: str, **_kw: Any) -> bool:
        self.deletes.append(path)
        self.dirs.add(str(PurePosixPath(path).parent))  # the directory stays
        return self.files.pop(path, None) is not None


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    root = tmp_path / "golden-repos"
    (root / "aliases").mkdir(parents=True)
    # A decoy on THIS pod's disk: never to be read under delegation.
    (root / "aliases" / f"{ALIAS}.json").write_text(json.dumps(_doc("/local/decoy")))
    return root


@pytest.fixture
def wsmcp(monkeypatch: pytest.MonkeyPatch, golden: Path) -> _WorkspaceMcp:
    seam = _WorkspaceMcp()
    seam.files[str(golden / "aliases" / f"{ALIAS}.json")] = json.dumps(
        _doc("/shared/v_1")
    ).encode()
    monkeypatch.setattr(content_wiring, "server_content_backend", lambda: seam)
    monkeypatch.setattr(writer_wiring, "server_content_writer", lambda: seam)
    return seam


@pytest.fixture(autouse=True)
def _restore_installed() -> Iterator[None]:
    previous = installed_control_plane_metadata()
    yield
    if previous is None:
        uninstall_control_plane_metadata()
    else:
        install_control_plane_metadata(previous)


class TestSeamMetadataFiles:
    def test_read_comes_from_workspace_mcp_not_local_disk(self, golden, wsmcp):
        files = SeamMetadataFiles(golden)

        document = files.read(KIND_ALIAS, ALIAS)
        assert document is not None and document["target_path"] == "/shared/v_1"
        assert files.read(KIND_ALIAS, "absent-global") is None

    def test_list_all_batches_skips_corrupt_and_nested_files(self, golden, wsmcp):
        aliases = golden / "aliases"
        wsmcp.files[str(aliases / "other-global.json")] = json.dumps(
            _doc("/shared/other")
        ).encode()
        wsmcp.files[str(aliases / "broken-global.json")] = b"{not json"
        wsmcp.files[str(aliases / ".code-indexer" / "x.json")] = b"{}"
        files = SeamMetadataFiles(golden)

        listed = files.list_all(KIND_ALIAS)

        assert sorted(listed) == [ALIAS, "other-global"]
        assert listed[ALIAS]["target_path"] == "/shared/v_1"
        assert sorted(files.list_all(KIND_ALIAS, prefix="other")) == ["other-global"]
        assert files.names(KIND_ALIAS) == sorted(
            [ALIAS, "broken-global", "other-global"]
        )

    def test_writes_and_deletes_go_through_the_writer(self, golden, wsmcp):
        files = SeamMetadataFiles(golden)
        target = str(golden / "aliases" / "new-global.json")

        files.write(KIND_ALIAS, "new-global", _doc("/shared/new"))
        files.delete(KIND_ALIAS, "new-global")

        assert wsmcp.puts == [target] and wsmcp.deletes == [target]
        assert not (golden / "aliases" / "new-global.json").exists()

    def test_no_backend_yet_raises_instead_of_reading_local_disk(
        self, golden, monkeypatch
    ):
        monkeypatch.setattr(content_wiring, "server_content_backend", lambda: None)
        files = SeamMetadataFiles(golden)

        with pytest.raises(MetadataFilesUnavailableError):
            files.read(KIND_ALIAS, ALIAS)
        with pytest.raises(OSError):  # what the startup import reports + retries
            files.list_all(KIND_ALIAS)

    def test_a_directory_not_created_yet_lists_as_empty(self, golden, wsmcp):
        # .write_mode exists only after the first write session.
        files = SeamMetadataFiles(golden)

        assert files.names(KIND_WRITE_MODE) == []
        assert files.list_all(KIND_WRITE_MODE) == {}

    def test_an_existing_empty_directory_lists_as_empty(self, golden, wsmcp):
        files = SeamMetadataFiles(golden)
        files.delete(KIND_ALIAS, ALIAS)

        assert files.names(KIND_ALIAS) == []
        assert files.list_all(KIND_ALIAS) == {}

    @pytest.mark.parametrize(
        "error",
        [
            _HttpError(503, "Service Unavailable"),
            _HttpError(403, "source_path is not under an allowed root"),
            _HttpError(400, "source_path is not a directory: /x"),
            ConnectionError("/content/manifest unreachable"),
        ],
        ids=["5xx", "forbidden", "other-400", "unreachable"],
    )
    def test_an_outage_is_never_an_empty_listing(self, golden, wsmcp, error):
        wsmcp.manifest_error = error
        files = SeamMetadataFiles(golden)

        with pytest.raises(MetadataFilesUnavailableError):
            files.names(KIND_WRITE_MODE)
        with pytest.raises(MetadataFilesUnavailableError):
            files.list_all(KIND_ALIAS)

    def test_alias_manager_resolves_through_the_seam(self, golden, wsmcp):
        metadata = ControlPlaneMetadata(golden, files=SeamMetadataFiles(golden))

        manager = AliasManager(golden / "aliases", metadata=metadata)

        assert manager.read_alias(ALIAS) == "/shared/v_1"


class _Registry:
    def __init__(self) -> None:
        self.imported: Dict[str, Dict[str, Any]] = {}

    def import_if_newer(self, kind: str, name: str, doc: Any, stamp: str) -> bool:
        self.imported[name] = doc
        return True


def _registry() -> Any:
    return cast(Any, _Registry())


class TestStartupImport:
    def test_import_reads_the_shared_files(self, golden, wsmcp):
        registry = _Registry()
        metadata = ControlPlaneMetadata(
            golden, backend=cast(Any, registry), files=SeamMetadataFiles(golden)
        )

        report = metadata.import_files()

        assert registry.imported[ALIAS]["target_path"] == "/shared/v_1"
        assert report["failed"] == 0 and metadata.import_complete

    def test_import_completes_before_write_mode_exists(self, golden, wsmcp):
        assert not any(".write_mode" in path for path in wsmcp.files)
        metadata = ControlPlaneMetadata(
            golden, backend=_registry(), files=SeamMetadataFiles(golden)
        )

        report = metadata.import_files()

        assert report["failed"] == 0 and metadata.import_complete

    def test_manifest_outage_is_a_failed_import(self, golden, wsmcp):
        wsmcp.manifest_error = _HttpError(503, "Service Unavailable")
        metadata = ControlPlaneMetadata(
            golden, backend=_registry(), files=SeamMetadataFiles(golden)
        )

        report = metadata.import_files()

        assert report["failed"] >= 1
        assert not metadata.import_complete, "the read-through must stay on"

    def test_unreachable_seam_is_a_failed_import_not_an_empty_one(
        self, golden, monkeypatch
    ):
        monkeypatch.setattr(content_wiring, "server_content_backend", lambda: None)
        metadata = ControlPlaneMetadata(
            golden, backend=_registry(), files=SeamMetadataFiles(golden)
        )

        report = metadata.import_files()

        assert report["failed"] >= 1
        assert not metadata.import_complete, "the read-through must stay on"


class _ReadRegistry(_Registry):
    """A registry that also answers reads (get / list_all)."""

    def __init__(self, rows: Dict[str, Dict[str, Any]]) -> None:
        super().__init__()
        self.rows = rows

    def get(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        return self.rows.get(name)

    def list_all(self, kind: str, prefix: str = "") -> Dict[str, Dict[str, Any]]:
        return {k: v for k, v in self.rows.items() if k.startswith(prefix)}


class _FetchErrorSeam(_WorkspaceMcp):
    """Lists every file but fails fetching the ones named in ``errors``."""

    def __init__(self, errors: Dict[str, str]) -> None:
        super().__init__()
        self.errors = errors

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        for item in super().fetch(repo_path, paths):
            error = self.errors.get(item.path)
            yield FetchedFile(item.path, None, b"", error=error) if error else item


def _seam_with_fetch_errors(monkeypatch, golden: Path, errors: Dict[str, str]):
    seam = _FetchErrorSeam(errors)
    for name in ("good-global", "gone-global", "dir-global", "bad-global"):
        seam.files[str(golden / "aliases" / f"{name}.json")] = json.dumps(
            _doc(f"/shared/{name}")
        ).encode()
    monkeypatch.setattr(content_wiring, "server_content_backend", lambda: seam)
    monkeypatch.setattr(writer_wiring, "server_content_writer", lambda: seam)
    return seam


class TestListAllFetchErrors:
    """list_all: an absent file is skipped, an unreadable one warned and
    skipped, and any OTHER per-file fetch error raises -- never a silent drop."""

    def test_absent_and_unreadable_files_are_skipped(self, golden, monkeypatch):
        _seam_with_fetch_errors(
            monkeypatch,
            golden,
            {"gone-global.json": "ENOENT", "dir-global.json": "EISDIR"},
        )
        listed = SeamMetadataFiles(golden).list_all(KIND_ALIAS)
        assert sorted(listed) == ["bad-global", "good-global"]

    @pytest.mark.parametrize("error", ["EIO", "ETIMEDOUT", "EPIPE"])
    def test_an_unexpected_fetch_error_raises(self, golden, monkeypatch, error):
        _seam_with_fetch_errors(monkeypatch, golden, {"bad-global.json": error})
        with pytest.raises(MetadataFilesUnavailableError, match=error):
            SeamMetadataFiles(golden).list_all(KIND_ALIAS)

    def test_a_failing_fetch_call_raises(self, golden, wsmcp, monkeypatch):
        def _boom(*_a: Any, **_k: Any) -> Iterator[FetchedFile]:
            raise ConnectionError("fetch unreachable")

        monkeypatch.setattr(wsmcp, "fetch", _boom)
        with pytest.raises(MetadataFilesUnavailableError, match="unreachable"):
            SeamMetadataFiles(golden).list_all(KIND_ALIAS)


_REGISTRY_ROWS = {"registry-global": _doc("/registry/v_1")}


def _unreachable_metadata(golden: Path, monkeypatch, *, settings=MetadataSettings):
    """Registry-backed metadata whose files' store (workspace-mcp) is down."""
    monkeypatch.setattr(content_wiring, "server_content_backend", lambda: None)
    return ControlPlaneMetadata(
        golden,
        backend=cast(Any, _ReadRegistry(dict(_REGISTRY_ROWS))),
        files=SeamMetadataFiles(golden),
        settings=settings,
    )


class TestReadThroughWithoutWorkspaceMcp:
    """Once start_import gives up, the read-through to the files stays on; a
    workspace-mcp outage then must not fail reads the registry can serve."""

    def test_the_import_gives_up_and_reads_still_answer(self, golden, monkeypatch):
        metadata = _unreachable_metadata(golden, monkeypatch)
        metadata.start_import(retry_delays=()).join(timeout=10)

        assert not metadata.import_complete
        assert metadata.get(KIND_ALIAS, "absent-global") is None
        assert (
            metadata.get(KIND_ALIAS, "registry-global")
            == _REGISTRY_ROWS["registry-global"]
        )
        assert metadata.list_all(KIND_ALIAS) == _REGISTRY_ROWS
        assert metadata.names(KIND_ALIAS) == ["registry-global"]

    def test_the_outage_is_logged_once_per_kind_at_warning(
        self, golden, monkeypatch, caplog
    ):
        metadata = _unreachable_metadata(golden, monkeypatch)
        with caplog.at_level("DEBUG"):
            metadata.get(KIND_ALIAS, "absent-global")
            metadata.list_all(KIND_ALIAS)
            metadata.list_all(KIND_WRITE_MODE)
        levels = [
            (r.levelname, r.args[0])
            for r in caplog.records
            if "read-through" in r.getMessage()
        ]
        assert levels == [
            ("WARNING", KIND_ALIAS),
            ("DEBUG", KIND_ALIAS),
            ("WARNING", KIND_WRITE_MODE),
        ]

    def test_a_reachable_store_still_reads_through(self, golden, wsmcp):
        metadata = ControlPlaneMetadata(
            golden,
            backend=cast(Any, _ReadRegistry(dict(_REGISTRY_ROWS))),
            files=SeamMetadataFiles(golden),
        )
        assert metadata.get(KIND_ALIAS, ALIAS)["target_path"] == "/shared/v_1"
        assert sorted(metadata.list_all(KIND_ALIAS)) == [ALIAS, "registry-global"]

    def test_files_mode_still_fails_loud(self, golden, monkeypatch):
        metadata = _unreachable_metadata(
            golden,
            monkeypatch,
            settings=lambda: MetadataSettings(read_source="files"),
        )
        with pytest.raises(MetadataFilesUnavailableError):
            metadata.get(KIND_ALIAS, "registry-global")
        with pytest.raises(MetadataFilesUnavailableError):
            metadata.list_all(KIND_ALIAS)

    def test_other_errors_are_not_swallowed(self, golden, monkeypatch):
        metadata = ControlPlaneMetadata(
            golden,
            backend=cast(Any, _ReadRegistry({})),
            files=SeamMetadataFiles(golden),
        )

        def _bug(*_a: Any, **_k: Any) -> Any:
            raise ValueError("a bug, not an outage")

        monkeypatch.setattr(metadata.files, "read", _bug)
        with pytest.raises(ValueError):
            metadata.get(KIND_ALIAS, "absent-global")


class TestWiring:
    def _install(self, golden: Path, content_backend: str) -> ControlPlaneMetadata:
        metadata = install_registry_metadata(
            SimpleNamespace(control_plane_metadata=_Registry()),
            golden,
            storage_mode="postgres",
            settings=MetadataSettings,
            start_import=False,
            content_backend=content_backend,
        )
        assert metadata is not None
        return metadata

    def test_delegated_installs_the_seam_files(self, golden) -> None:
        assert isinstance(self._install(golden, "delegated").files, SeamMetadataFiles)

    def test_local_default_keeps_the_disk_files(self, golden) -> None:
        assert type(self._install(golden, "local").files) is MetadataFiles

    def test_startup_passes_the_configured_content_backend(self) -> None:
        import inspect

        from code_indexer.server.startup import service_init

        source = inspect.getsource(service_init.initialize_services)
        assert "content_backend=_content_backend_name(server_config)" in source


class TestMetadataForResolvesSpellings:
    """Several server sites derive golden-repos from a realpath'd sibling
    (``activated_repos_dir``); a lexical-only match handed them files-only
    (local disk) metadata whenever the data dir sits behind a symlink."""

    def _install(self, golden: Path) -> ControlPlaneMetadata:
        metadata = ControlPlaneMetadata(golden, backend=_registry())
        install_control_plane_metadata(metadata)
        return metadata

    def test_a_symlinked_spelling_gets_the_installed_component(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.global_repos.control_plane_metadata import metadata_for

        real = tmp_path / "real" / "golden-repos"
        real.mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "real")
        installed = self._install(link / "golden-repos")

        assert metadata_for(real) is installed
        assert metadata_for(link / "golden-repos") is installed

    def test_a_misrouted_directory_is_files_only_and_logged_once(
        self, tmp_path: Path, caplog
    ) -> None:
        from code_indexer.global_repos.control_plane_metadata import metadata_for

        self._install(tmp_path / "golden-repos")
        other = tmp_path / "elsewhere" / "golden-repos"

        with caplog.at_level("WARNING"):
            first = metadata_for(other)
            metadata_for(other)

        assert not first.is_registry_backed
        warnings = [r for r in caplog.records if "files-only" in r.getMessage()]
        assert len(warnings) == 1


class TestIsAbsentRootError:
    """The narrow match that lets an absent directory list as empty."""

    def test_the_workspace_mcp_answer_matches(self) -> None:
        assert is_absent_root_error(_HttpError(400, "source_path does not exist: /a"))

    def test_it_matches_through_the_wrapping_chain(self) -> None:
        try:
            try:
                raise _HttpError(400, "source_path does not exist: /a")
            except _HttpError as inner:
                raise OSError("listing the shared content failed") from inner
        except OSError as outer:
            assert is_absent_root_error(outer)

    def test_status_and_marker_are_both_required(self) -> None:
        assert not is_absent_root_error(_HttpError(400, "invalid cursor"))
        assert not is_absent_root_error(
            _HttpError(500, "source_path does not exist: /a")
        )
        assert not is_absent_root_error(RuntimeError("source_path does not exist: /a"))

    def test_without_a_detail_the_message_is_read(self) -> None:
        error = RuntimeError("returned 400: source_path does not exist: /a")
        setattr(error, "status", 400)

        assert is_absent_root_error(error)
