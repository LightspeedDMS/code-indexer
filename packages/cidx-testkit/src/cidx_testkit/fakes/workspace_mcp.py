"""An in-process fake of workspace-mcp, and delegate-shaped clients for it.

workspace-mcp is the remote service several seams delegate to: content
(manifest + fetch), content writes, ripgrep, clone and index artifacts. The
real delegates live outside this repository; this fake reproduces the
service's ANSWERS -- including the ones a local backend never gives -- so a
seam's conformance suite and the core's delegated code paths can run against
something with the remote shape:

* its files are not this process's business: clients report ``is_local``
  False, and the service maps the roots it is asked about onto its own store;
* a manifest of a root it does not hold is HTTP 400 ``source_path does not
  exist`` (:class:`FakeServiceError`), never an empty listing;
* a file under a missing directory is EACCES, not ENOENT;
* writes take a compare-and-swap ``expected_blob_sha`` and fail with ``code``
  ECONFLICT;
* every request is recorded (:attr:`FakeWorkspaceMcp.requests`).

The listing is implemented independently of any local backend (a plain sorted
walk), so passing the content suite against it says something.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from cidx_sdk.entry_points import (
    CLONE_BACKEND_ENTRY_POINT_GROUP,
    CONTENT_BACKEND_ENTRY_POINT_GROUP,
    CONTENT_WRITER_ENTRY_POINT_GROUP,
    INDEX_ARTIFACTS_ENTRY_POINT_GROUP,
    RIPGREP_BACKEND_ENTRY_POINT_GROUP,
)
from cidx_sdk.seams.content import (
    ABSENT_ROOT_MARKER,
    ABSENT_ROOT_STATUS,
    DEFAULT_PAGE_SIZE,
    MANIFEST_OPTIONS,
    REFUSED_SYMLINK,
    REFUSED_SYMLINKED_DIRECTORY,
    ContentEntry,
    ContentPage,
    FetchedFile,
    entry_in_scope,
    normalize_manifest_prefix,
    unsafe_relative_reason,
)
from cidx_sdk.seams.clone import (
    SHORT_POLL_SECONDS,
    CloneBackendSettings,
    CloneDestinationExistsError,
    SubprocessCancelledError,
)
from cidx_sdk.seams.content_identity import git_blob_hash_bytes
from cidx_sdk.seams.content_writer import (
    ECONFLICT,
    EFORBIDDEN,
    EINVAL,
    UNSET as UNSET,
    ContentWriteError,
)
from cidx_sdk.seams.ripgrep import RipgrepRun
from cidx_sdk.seams.resolver import (
    EntryPointLike,
    add_entry_points,
    extra_entry_points,
    remove_entry_points,
)
from cidx_sdk.seams.selection import DELEGATED_MODE


class FakeServiceError(Exception):
    """A non-2xx answer: ``status`` and the response ``detail``, as a delegate
    plugin raises it."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class FakeWriteError(ContentWriteError):
    """A content-writer failure carrying the writer ``code``."""


@dataclass
class FakeWorkspaceMcp:
    """The service. ``store`` is where it keeps repositories; a request names a
    repository by the path cidx knows it under, mapped through ``roots``
    (cidx path -> directory in ``store``) or, by default, taken as-is."""

    store: Path
    roots: Dict[str, Path] = field(default_factory=dict)
    requests: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    artifacts: Dict[str, List[Tuple[str, Path]]] = field(default_factory=dict)

    # -- addressing ------------------------------------------------------------

    def serve(self, cidx_path: str, directory: Optional[Path] = None) -> Path:
        """Hold a repository known to cidx as *cidx_path*; returns its directory."""
        target = directory or (self.store / f"repo{len(self.roots)}")
        target.mkdir(parents=True, exist_ok=True)
        self.roots[cidx_path] = target
        return target

    def _repo(self, source_path: Union[str, Path]) -> Path:
        key = str(source_path)
        directory = self.roots.get(key, Path(key))
        if not directory.is_dir():
            raise FakeServiceError(
                ABSENT_ROOT_STATUS, f"{ABSENT_ROOT_MARKER}: {source_path}"
            )
        return directory

    def _record(self, route: str, **body: Any) -> None:
        self.requests.append((route, body))

    # -- POST /content/manifest ------------------------------------------------

    def content_manifest(
        self,
        source_path: Union[str, Path],
        *,
        cursor: Optional[str] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        prune_dirs: Sequence[str] = (),
        hash: bool = True,
        prefix: Optional[str] = None,
        max_depth: Optional[int] = None,
    ) -> ContentPage:
        self._record(
            "/content/manifest",
            source_path=str(source_path),
            cursor=cursor,
            page_size=page_size,
            prune_dirs=list(prune_dirs),
            hash=hash,
            prefix=prefix,
            max_depth=max_depth,
        )
        repo = self._repo(source_path)
        scope = normalize_manifest_prefix(prefix)
        if max_depth is not None and max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        if cursor is not None:
            unsafe = unsafe_relative_reason(cursor)
            if unsafe is not None:
                raise ValueError(f"invalid manifest cursor: {unsafe}")
        pruned = set(prune_dirs) | {".git"}
        paths = sorted(
            rel
            for rel in self._walk(repo, pruned)
            if entry_in_scope(rel, scope, max_depth)
        )
        if cursor is not None:
            paths = [p for p in paths if p > cursor]
        page = paths[:page_size]
        entries = tuple(self._describe(repo, rel, hash) for rel in page)
        more = len(paths) > page_size
        return ContentPage(entries=entries, next_cursor=page[-1] if more else None)

    @staticmethod
    def _walk(repo: Path, pruned: set) -> Iterator[str]:
        for current, dirs, files in os.walk(repo, followlinks=False):
            base = Path(current)
            kept = []
            for name in dirs:
                if name in pruned:
                    continue
                if (base / name).is_symlink():
                    yield (base / name).relative_to(repo).as_posix()
                    continue
                kept.append(name)
            dirs[:] = kept
            for name in files:
                yield (base / name).relative_to(repo).as_posix()

    @staticmethod
    def _describe(repo: Path, rel: str, with_hash: bool) -> ContentEntry:
        target = repo / rel
        stat = target.lstat()
        if target.is_symlink():
            return ContentEntry(rel, stat.st_size, None, True, stat.st_mtime_ns)
        digest = git_blob_hash_bytes(target.read_bytes()) if with_hash else None
        return ContentEntry(rel, stat.st_size, digest, False, stat.st_mtime_ns)

    # -- POST /content/fetch ---------------------------------------------------

    def content_fetch(
        self, source_path: Union[str, Path], paths: Sequence[str]
    ) -> List[FetchedFile]:
        self._record("/content/fetch", source_path=str(source_path), paths=list(paths))
        repo = self._repo(source_path)
        return [self._fetch_one(repo, rel) for rel in paths]

    @staticmethod
    def _fetch_one(repo: Path, rel: str) -> FetchedFile:
        unsafe = unsafe_relative_reason(rel)
        if unsafe is not None:
            return FetchedFile(rel, None, b"", unsafe)
        target = repo / rel
        parent = repo
        for part in rel.split("/")[:-1]:
            parent = parent / part
            if parent.is_symlink():
                return FetchedFile(rel, None, b"", REFUSED_SYMLINKED_DIRECTORY)
            if not parent.is_dir():
                # workspace-mcp answers EACCES for a file under a missing dir.
                return FetchedFile(rel, None, b"", "EACCES")
        if target.is_symlink():
            return FetchedFile(rel, None, b"", REFUSED_SYMLINK)
        if target.is_dir():
            return FetchedFile(rel, None, b"", "EISDIR")
        if not target.exists():
            return FetchedFile(rel, None, b"", "ENOENT")
        data = target.read_bytes()
        mtime = target.stat().st_mtime_ns
        return FetchedFile(rel, git_blob_hash_bytes(data), data, None, mtime)

    # -- POST /content/write ---------------------------------------------------

    def content_write(
        self, path: str, data: Optional[bytes], expected_blob_sha: Any = UNSET
    ) -> Optional[str]:
        """Write (``data``) or delete (``data is None``) an ABSOLUTE path."""
        self._record(
            "/content/write",
            path=path,
            delete=data is None,
            conditional=expected_blob_sha is not UNSET,
        )
        target = self._absolute(path)
        if ".git" in target.parts:
            raise FakeWriteError(EFORBIDDEN, "writes under .git are refused")
        if target.is_symlink():
            raise FakeWriteError(EFORBIDDEN, f"{path} is a symlink")
        current = git_blob_hash_bytes(target.read_bytes()) if target.is_file() else None
        if expected_blob_sha is not UNSET and expected_blob_sha != current:
            raise FakeWriteError(ECONFLICT, f"{path} is not at the expected blob")
        if data is None:
            if not target.exists():
                return None
            target.unlink()
            return current
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return git_blob_hash_bytes(data)

    def _absolute(self, path: str) -> Path:
        if not os.path.isabs(path):
            raise FakeWriteError(EINVAL, "the writer takes absolute paths")
        for cidx_root, directory in self.roots.items():
            prefix = cidx_root.rstrip("/") + "/"
            if path.startswith(prefix):
                return directory / path[len(prefix) :]
        return Path(path)

    # -- POST /content/rg ------------------------------------------------------

    def content_rg(
        self, argv: Sequence[str], working_dir: str, timeout_seconds: float
    ) -> Tuple[Optional[int], bytes, str]:
        """Run ripgrep over the repository the service holds (needs ``rg``)."""
        self._record("/content/rg", argv=list(argv), working_dir=working_dir)
        repo = self._repo(working_dir)
        binary = shutil.which("rg")
        if binary is None:
            raise FakeServiceError(503, "ripgrep is not installed")
        args = list(argv[1:]) if argv and argv[0].endswith("rg") else list(argv)
        try:
            done = subprocess.run(
                [binary, *args],
                cwd=repo,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, b"", "timeout"
        return done.returncode, done.stdout, done.stderr.decode("utf-8", "replace")

    # -- POST /clone-path ------------------------------------------------------

    def clone_path(self, source: str, dest: str) -> str:
        self._record("/clone-path", source=source, dest=dest)
        target = self._absolute(dest)
        if target.exists():
            raise FakeServiceError(409, f"destination exists: {dest}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self._repo(source), target, symlinks=True)
        return dest

    # -- POST /clone-path/stat, /clone-path/delete, /clone-path/move -----------

    def clone_stat(self, path: str) -> Dict[str, bool]:
        self._record("/clone-path/stat", path=path)
        target = self._absolute(path)
        return {"exists": target.exists(), "is_dir": target.is_dir()}

    def clone_delete(self, path: str) -> bool:
        """Remove the tree at *path*; a missing path is the service's 404
        ``ENOENT`` answer (:class:`FakeServiceError`)."""
        self._record("/clone-path/delete", path=path)
        target = self._absolute(path)
        if not target.exists():
            raise FakeServiceError(404, f"ENOENT: no such file or directory: {path}")
        shutil.rmtree(target)
        return True

    def clone_move(self, source: str, dest: str) -> bool:
        self._record("/clone-path/move", source=source, dest=dest)
        origin = self._absolute(source)
        target = self._absolute(dest)
        if not origin.exists():
            raise FakeServiceError(404, f"ENOENT: no such file or directory: {source}")
        if target.exists():
            raise FakeServiceError(409, f"destination exists: {dest}")
        origin.rename(target)
        return True

    # -- index artifacts -----------------------------------------------------
    #
    # The artifact service's API as the index-artifacts client exposes it:
    # versions are immutable, a publish names the version it builds on
    # (compare-and-swap: a concurrent publish in between is refused with 409),
    # and a checkout writes a version into a directory that must not exist.

    def current_artifact_version(self, alias: str) -> Optional[str]:
        self._record("/index/current", alias=alias)
        versions = self.artifacts.get(alias) or []
        return versions[-1][0] if versions else None

    def publish_artifact(
        self, alias: str, base_version: Optional[str], built_dir: Path
    ) -> str:
        self._record("/index/publish", alias=alias, base_version=base_version)
        versions = self.artifacts.setdefault(alias, [])
        current = versions[-1][0] if versions else None
        if base_version != current:
            raise FakeServiceError(
                409, f"{alias} is at {current}, not at base {base_version}"
            )
        version = f"v{len(versions) + 1}"
        kept = self.store / "artifacts" / alias / version
        shutil.copytree(built_dir, kept)
        versions.append((version, kept))
        return version

    def checkout_artifact(self, alias: str, dest: Path, version: Optional[str]) -> str:
        self._record("/index/checkout", alias=alias, version=version)
        versions = self.artifacts.get(alias) or []
        if not versions:
            raise FakeServiceError(404, f"no published index for {alias}")
        chosen_version = version or versions[-1][0]
        chosen = dict(versions).get(chosen_version)
        if chosen is None:
            raise FakeServiceError(404, f"{alias} has no version {version}")
        if dest.exists():
            raise FakeServiceError(409, f"checkout destination exists: {dest}")
        shutil.copytree(chosen, dest)
        return chosen_version

    def delete_artifacts(self, alias: str) -> int:
        self._record("/index/delete", alias=alias)
        return len(self.artifacts.pop(alias, []))


# ── delegate-shaped clients ───────────────────────────────────────────────────


class FakeWorkspaceMcpContentBackend:
    """A content backend that reads through :class:`FakeWorkspaceMcp`."""

    manifest_options = MANIFEST_OPTIONS
    manifest_options_supported = True

    def __init__(self, service: FakeWorkspaceMcp) -> None:
        self.service = service

    @property
    def is_local(self) -> bool:
        return False

    def manifest(
        self,
        repo_path: Union[str, Path],
        *,
        cursor: Optional[str] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        prune_dirs: Optional[Sequence[str]] = None,
        hash: bool = True,
        prefix: Optional[str] = None,
        max_depth: Optional[int] = None,
    ) -> ContentPage:
        return self.service.content_manifest(
            repo_path,
            cursor=cursor,
            page_size=page_size,
            prune_dirs=tuple(prune_dirs or ()),
            hash=hash,
            prefix=prefix,
            max_depth=max_depth,
        )

    def fetch(
        self, repo_path: Union[str, Path], paths: Sequence[str]
    ) -> Iterator[FetchedFile]:
        yield from self.service.content_fetch(repo_path, list(paths))


class FakeWorkspaceMcpContentWriter:
    """A content writer that writes through :class:`FakeWorkspaceMcp`."""

    def __init__(self, service: FakeWorkspaceMcp) -> None:
        self.service = service

    def put(self, path: str, data: bytes, *, expected_blob_sha: Any = UNSET) -> object:
        return self.service.content_write(path, data, expected_blob_sha)

    def delete(self, path: str, *, expected_blob_sha: Any = UNSET) -> bool:
        return self.service.content_write(path, None, expected_blob_sha) is not None


class FakeWorkspaceMcpRipgrepBackend:
    """A ripgrep backend that runs ``rg`` in :class:`FakeWorkspaceMcp`."""

    def __init__(self, service: FakeWorkspaceMcp) -> None:
        self.service = service

    @property
    def is_local(self) -> bool:
        return False

    async def run(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> RipgrepRun:
        code, out, err = self.service.content_rg(argv, working_dir, timeout_seconds)
        capped = len(out) > max_output_bytes
        Path(output_file_path).write_bytes(out[:max_output_bytes])
        return RipgrepRun(
            exit_code=code,
            stderr_output=err,
            timed_out=code is None,
            timeout_seconds=timeout_seconds,
            is_error=code is None or code > 1 or capped,
            output_capped=capped,
        )

    async def supports_pcre2(self) -> bool:
        return False


class FakeIndexArtifacts:
    """An index-artifacts client over :class:`FakeWorkspaceMcp`, in the shape
    the core's ``DelegatedIndexStore`` drives (``current_version``,
    ``publish(alias, base_version, source_dir)``,
    ``checkout(alias, dest_dir, version)``, ``delete_alias``)."""

    def __init__(self, service: FakeWorkspaceMcp) -> None:
        self.service = service

    def current_version(self, alias: str) -> Optional[str]:
        return self.service.current_artifact_version(alias)

    def publish(self, alias: str, base_version: Optional[str], source_dir: str) -> str:
        return self.service.publish_artifact(alias, base_version, Path(source_dir))

    def checkout(self, alias: str, dest_dir: str, version: Optional[str] = None) -> str:
        return self.service.checkout_artifact(alias, Path(dest_dir), version)

    def delete_alias(self, alias: str) -> int:
        return self.service.delete_artifacts(alias)


@dataclass(frozen=True)
class _Registration:
    name: str
    factory: Any

    def load(self) -> Any:
        return self.factory


def register_as_delegate(service: FakeWorkspaceMcp) -> Dict[str, List[str]]:
    """Register the fake's clients as the ``delegated`` provider of the
    content, content writer, ripgrep and index artifacts seams, exactly as an
    installed delegate distribution would be found. Returns what to pass to
    :func:`unregister_delegate`."""
    factories = {
        CONTENT_BACKEND_ENTRY_POINT_GROUP: lambda: FakeWorkspaceMcpContentBackend(
            service
        ),
        CONTENT_WRITER_ENTRY_POINT_GROUP: lambda: FakeWorkspaceMcpContentWriter(
            service
        ),
        RIPGREP_BACKEND_ENTRY_POINT_GROUP: lambda: FakeWorkspaceMcpRipgrepBackend(
            service
        ),
        INDEX_ARTIFACTS_ENTRY_POINT_GROUP: lambda: FakeIndexArtifacts(service),
    }
    for group, factory in factories.items():
        add_entry_points(group, [_Registration(DELEGATED_MODE, factory)])
    return {group: [DELEGATED_MODE] for group in factories}


def unregister_delegate(registered: Dict[str, List[str]]) -> None:
    """Withdraw what :func:`register_as_delegate` added."""
    for group, names in registered.items():
        remove_entry_points(group, names)


# ── the clone seam: workspace-mcp's clone-path routes ─────────────────────────


class FakeWorkspaceMcpCloneBackend:
    """A clone backend (``cidx_sdk.seams.clone.CloneBackend``) whose every
    operation is a request to :class:`FakeWorkspaceMcp`'s clone-path routes.

    Snapshots live at ``<versioned_base>/.versioned/<namespace>/<name>``, the
    same layout as every other clone backend.
    """

    def __init__(self, service: FakeWorkspaceMcp, versioned_base: str = "") -> None:
        self.service = service
        self.versioned_base = versioned_base

    def _snapshot(self, namespace: str, name: str) -> str:
        if not self.versioned_base:
            raise RuntimeError("this clone backend was built without versioned_base")
        for value in (namespace, name):
            if not value or "/" in value or value in (".", ".."):
                raise ValueError(f"invalid clone identifier {value!r}")
        return f"{self.versioned_base.rstrip('/')}/.versioned/{namespace}/{name}"

    def create_clone(self, source_path: str, namespace: str, name: str) -> str:
        return self.service.clone_path(source_path, self._snapshot(namespace, name))

    def create_clone_at_path(
        self,
        source_path: str,
        dest_path: str,
        preserve_attrs: bool = True,
        timeout: Optional[int] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        poll_interval: float = SHORT_POLL_SECONDS,
    ) -> str:
        if cancel_check is not None and cancel_check():
            raise SubprocessCancelledError("clone cancelled before the request")
        try:
            return self.service.clone_path(source_path, dest_path)
        except FakeServiceError as exc:
            if exc.status == 409:
                raise CloneDestinationExistsError(str(exc)) from exc
            raise

    def delete_clone(self, clone_path: str) -> bool:
        try:
            return self.service.clone_delete(clone_path)
        except FakeServiceError as exc:
            if exc.status == 404:
                return True
            raise

    def list_clones(self, namespace: str) -> List[dict]:
        base = self._snapshot(namespace, "probe").rsplit("/", 1)[0]
        directory = self.service._absolute(base)
        if not directory.is_dir():
            return []
        return [
            {
                "namespace": namespace,
                "name": entry.name,
                "clone_path": f"{base}/{entry.name}",
            }
            for entry in sorted(directory.iterdir())
            if entry.is_dir()
        ]

    def clone_exists(self, namespace: str, name: str) -> bool:
        return self.service.clone_stat(self._snapshot(namespace, name))["exists"]


class FakeResponse:
    """The part of ``requests.Response`` the delegated clients read."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.ok = status_code < 400

    def json(self) -> Any:
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body

    def raise_for_status(self) -> None:
        if not self.ok:
            raise FakeRequestException(f"HTTP {self.status_code}: {self.text}")


class FakeRequestException(Exception):
    """``requests.RequestException`` as the fake transport raises it."""


class FakeWorkspaceMcpHttp:
    """A stand-in for the ``requests`` module that routes workspace-mcp's
    clone-path routes to :class:`FakeWorkspaceMcp`, so a REAL delegated
    client (one that speaks HTTP) runs against the fake service::

        monkeypatch.setitem(sys.modules, "requests", FakeWorkspaceMcpHttp(svc))

    Every request is answered the way the service answers: a
    :class:`FakeServiceError` becomes its status and detail.
    """

    RequestException = FakeRequestException

    def __init__(self, service: FakeWorkspaceMcp) -> None:
        self.service = service

    def post(
        self,
        url: str,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> FakeResponse:
        body = dict(json or {})
        route = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        try:
            return FakeResponse(200, self._answer(route, body))
        except FakeServiceError as exc:
            return FakeResponse(exc.status, exc.detail)

    def _answer(self, route: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if route == "/clone-path":
            dest = self.service.clone_path(body["source_path"], body["dest_path"])
            return {"dest_path": dest, "method": "copy"}
        if route == "/clone-path/stat":
            return dict(self.service.clone_stat(body["path"]))
        if route == "/clone-path/delete":
            return {"deleted": self.service.clone_delete(body["path"])}
        if route == "/clone-path/move":
            self.service.clone_move(body["from"], body["to"])
            return {"from": body["from"], "to": body["to"], "moved": True}
        raise FakeServiceError(404, f"Cannot POST {route}")


@dataclass(frozen=True)
class CloneDelegateRegistration:
    """What :func:`register_clone_delegate` replaced (an in-tree ``delegated``
    clone provider, or None), so unregistering restores it."""

    previous: Optional[EntryPointLike]


def register_clone_delegate(service: FakeWorkspaceMcp) -> CloneDelegateRegistration:
    """Register :class:`FakeWorkspaceMcpCloneBackend` as the ``delegated``
    provider of the clone seam, exactly as an installed delegate is found. Its
    factory takes ``CloneBackendSettings`` like every clone provider. Replaces
    the ``delegated`` entry already registered (the core's own, in a source
    tree run); :func:`unregister_clone_delegate` puts that back."""
    previous = extra_entry_points().get(CLONE_BACKEND_ENTRY_POINT_GROUP, {})

    def factory(settings: CloneBackendSettings) -> FakeWorkspaceMcpCloneBackend:
        return FakeWorkspaceMcpCloneBackend(service, settings.versioned_base)

    registration = CloneDelegateRegistration(previous.get(DELEGATED_MODE))
    add_entry_points(
        CLONE_BACKEND_ENTRY_POINT_GROUP, [_Registration(DELEGATED_MODE, factory)]
    )
    return registration


def unregister_clone_delegate(registration: CloneDelegateRegistration) -> None:
    """Withdraw the fake clone delegate; restore what it replaced."""
    remove_entry_points(CLONE_BACKEND_ENTRY_POINT_GROUP, [DELEGATED_MODE])
    if registration.previous is not None:
        add_entry_points(CLONE_BACKEND_ENTRY_POINT_GROUP, [registration.previous])
