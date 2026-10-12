"""Multimodal images are read through the content seam on a pod without the repository.

Found as a follow-up to the seam backlog (!160): with content, git, ripgrep and
the index store delegated, a document's image references were checked with a
LOCAL ``exists()`` in ``FileChunkingManager`` and their bytes read with a local
``open`` in ``encode_image_to_base64``. On a pod without the repository volume
the first is False for every image, so every multimodal chunk lost every image
and was skipped -- a markdown file whose only chunk carried images then failed
outright ("No batches created"), and the multimodal collection stayed empty.

Pinned here, against that shape (an empty directory on this pod, the
repository served from elsewhere):

* **Identity.** The delegated multimodal point set -- ids, payloads and the
  vectors, which a fake vendor derives from the exact request (text and every
  image's base64 bytes) -- equals the local one on the same repository.
* **Call shape.** Image bytes come a planned batch at a time:
  ``ceil(N / page)`` fetches for N images, never one per image, and a second
  reference to an image costs no fetch.
* **No mount.** The pod's directory holds none of the files.
* **Local unchanged.** Without a delegated manifest no image source exists and
  the client is called exactly as before.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from code_indexer.indexing.content_prefetcher import ImagePrefetcher
from code_indexer.indexing.file_facts import ContentUnavailableError
from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import (
    ContentPage,
    FetchedFile,
    LocalContentBackend,
)
from code_indexer.server.storage.shared.git_backend import LocalGitBackend
from code_indexer.services.voyage_multimodal import VoyageMultimodalClient
from code_indexer.services.multimodal_utils import (
    SUPPORTED_MEDIA_TYPES,
    encode_image_to_base64,
)
from tests.unit.services.test_reconcile_git_seam_mountless import (
    _Elsewhere,
    _Embedder,
)

REPO = "agent-router"
MULTIMODAL_COLLECTION = "voyage-multimodal-3"
#: Payload fields that record WHEN a point was written, not what it is.
_WRITE_TIME_FIELDS = ("indexed_at", "indexed_timestamp")

IMAGES = {
    "docs/images/a.png": b"\x89PNG-a" * 50,
    "images/b.jpg": b"\xff\xd8-b" * 70,
    "assets/c.gif": b"GIF89a-c" * 30,
    "docs/images/d.webp": b"RIFF-d" * 40,
    "docs/images/unreferenced.png": b"\x89PNG-u" * 20,
}
DOCS = {
    # Relative, root-absolute and parent-relative references; a remote URL and
    # a missing file, which both paths must skip alike.
    "docs/guide.md": (
        "# Guide\n\nThe architecture diagram:\n\n![arch](images/a.png)\n\n"
        "And the deploy flow ![flow](/images/b.jpg) plus ![c](../assets/c.gif).\n"
        "Remote ![r](https://example.com/r.png), missing ![m](images/nope.png).\n"
    ),
    "docs/page.html": (
        '<html><body><p>Overview</p><img src="images/d.webp"></body></html>\n'
    ),
    "docs/again.md": "Same diagram, second document: ![arch](images/a.png)\n",
    "src/app.py": "def main():\n    return 1\n",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


# ── fakes: the vendor and workspace-mcp ─────────────────────────────────────


class _Response:
    def __init__(self, request: Dict[str, Any]) -> None:
        self._request = request

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Dict[str, Any]:
        """A vector derived from the WHOLE request: text and image bytes alike."""
        key = json.dumps(self._request["inputs"], sort_keys=True)
        return {"data": [{"embedding": _Embedder._vector(key)}]}


class _Vendor:
    """Stands in for the multimodal HTTP API; records every request. No network."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []

    def create_sync_client(self, **kw: Any) -> "_Vendor":
        return self

    def __enter__(self) -> "_Vendor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def post(self, url: str, json: Dict[str, Any]) -> _Response:
        self.requests.append(json)
        return _Response(json)


class _WorkspaceMcp(LocalContentBackend):
    """The repository's files, served from elsewhere; every fetch recorded."""

    def __init__(self, elsewhere: _Elsewhere) -> None:
        self._elsewhere = elsewhere
        self.fetches: List[List[str]] = []

    @property
    def is_local(self) -> bool:
        return False

    def manifest(self, repo_path: Any, **kw: Any) -> ContentPage:
        return super().manifest(self._elsewhere(repo_path), **kw)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Any:
        self.fetches.append(list(paths))
        return super().fetch(self._elsewhere(repo_path), paths)


def _git_mcp(elsewhere: _Elsewhere) -> LocalGitBackend:
    class GitMcp(LocalGitBackend):
        @property
        def owns_credentials(self) -> bool:
            return True

        @staticmethod
        def _run(cmd: Sequence[str], cwd: Path, **kw: Any) -> Any:
            return LocalGitBackend._run(cmd, elsewhere(cwd), **kw)

    return GitMcp()


# ── end to end through the CLI ──────────────────────────────────────────────


@pytest.fixture
def held(tmp_path: Path) -> Path:
    """The repository, committed -- what workspace-mcp and git-mcp hold."""
    root = tmp_path / "workspace-mcp" / REPO
    for rel, data in {**IMAGES, **{k: v.encode() for k, v in DOCS.items()}}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    _git(root, "init", "-q", "-b", "master")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


def _index(
    monkeypatch: pytest.MonkeyPatch, here: Path, seams: Sequence[str]
) -> _Vendor:
    from code_indexer import cli as cli_module
    from code_indexer.services import embedding_factory, voyage_multimodal

    vendor = _Vendor()

    class FakeVendorClient(VoyageMultimodalClient):
        def __init__(self, config: Any, **kw: Any) -> None:
            super().__init__(config, http_client_factory=vendor)

    monkeypatch.setattr(voyage_multimodal, "VoyageMultimodalClient", FakeVendorClient)
    monkeypatch.setattr(
        embedding_factory.EmbeddingProviderFactory,
        "create",
        staticmethod(lambda *a, **k: _Embedder()),
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "not-used")
    monkeypatch.delenv("CIDX_MULTIMODAL_INDEXING_ENABLED", raising=False)
    monkeypatch.chdir(here)
    for args in (
        ["init", "--embedding-provider", "voyage-ai"],
        ["index", "--new-collection-layout=chunks_db", *seams],
    ):
        result = CliRunner().invoke(cli_module.cli, args, catch_exceptions=False)
        assert result.exit_code == 0, result.output
    return vendor


def _multimodal_points(here: Path) -> Dict[str, Tuple[Dict[str, Any], List[float]]]:
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    store = FilesystemVectorStore(here / ".code-indexer" / "index", project_root=here)
    points, _ = store.scroll_points(
        MULTIMODAL_COLLECTION, limit=1000, with_vectors=True
    )
    out = {}
    for point in points:
        payload = {
            k: v for k, v in point["payload"].items() if k not in _WRITE_TIME_FIELDS
        }
        out[point["id"]] = (payload, [round(x, 6) for x in point["vector"]])
    return out


def _canonical(requests: List[Dict[str, Any]]) -> List[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in requests)


def _local_run(tmp_path: Path, held: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    # A byte-for-byte copy (mtimes and .git included): the mounted checkout.
    local = tmp_path / "mounted" / REPO
    shutil.copytree(held, local, symlinks=True)
    vendor = _index(monkeypatch, local, ())
    return _multimodal_points(local), vendor


def _delegated_run(
    tmp_path: Path, held: Path, monkeypatch: pytest.MonkeyPatch
) -> Tuple[Dict[str, Any], _Vendor, _WorkspaceMcp, Path]:
    from code_indexer import cli as cli_module

    here = tmp_path / "pod" / REPO
    here.mkdir(parents=True)
    elsewhere = _Elsewhere(here, held)
    backend = _WorkspaceMcp(elsewhere)
    monkeypatch.setattr(
        cli_module, "_build_index_git_backend", lambda name: _git_mcp(elsewhere)
    )
    monkeypatch.setattr(
        cli_module, "_build_index_content_backend", lambda name: backend
    )
    vendor = _index(
        monkeypatch, here, ("--content-backend=delegated", "--git-backend=delegated")
    )
    return _multimodal_points(here), vendor, backend, here


def test_delegated_multimodal_points_equal_local(
    tmp_path: Path, held: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same ids, payloads and vectors -- relocation must not re-key or drop a point."""
    local, local_vendor = _local_run(tmp_path, held, monkeypatch)
    delegated, delegated_vendor, _, _ = _delegated_run(tmp_path, held, monkeypatch)

    # The fixture really exercises images: one point per image-bearing document.
    assert sorted(p["path"] for p, _ in local.values()) == [
        "docs/again.md",
        "docs/guide.md",
        "docs/page.html",
    ]
    assert delegated == local
    # The vendor saw the same requests: same text and the same image bytes
    # (workers finish in any order, so compared as a set).
    assert _canonical(delegated_vendor.requests) == _canonical(local_vendor.requests)


def test_a_pod_without_the_repository_fetches_images_in_batches(
    tmp_path: Path, held: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, vendor, backend, here = _delegated_run(tmp_path, held, monkeypatch)

    # No mount: nothing of the repository is on this pod.
    for rel in [*IMAGES, *DOCS]:
        assert not (here / rel).exists()
    assert not (here / ".git").exists()

    image_fetches = [
        call
        for call in backend.fetches
        if any(Path(p).suffix in SUPPORTED_MEDIA_TYPES for p in call)
    ]
    # Every image is planned into ONE batch (they are tiny): one fetch, though
    # four images are referenced -- one of them from two documents.
    assert len(image_fetches) == 1
    assert sorted(image_fetches[0]) == sorted(IMAGES)
    sent = json.dumps(vendor.requests)
    for rel in ("docs/images/a.png", "images/b.jpg", "assets/c.gif"):
        assert encode_image_to_base64(held / rel) in sent


# ── the image source itself ─────────────────────────────────────────────────


class _Recording(LocalContentBackend):
    """A delegated backend over a dict of files; records every fetch."""

    def __init__(self, files: Dict[str, bytes], fail: bool = False) -> None:
        self.files = files
        self.fail = fail
        self.fetches: List[List[str]] = []

    @property
    def is_local(self) -> bool:
        return False

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Any:
        self.fetches.append(list(paths))
        if self.fail:
            raise ConnectionError("workspace-mcp: 503")
        for path in paths:
            if path in self.files:
                yield FetchedFile(path=path, blob_sha=None, data=self.files[path])
            else:
                yield FetchedFile(path=path, blob_sha=None, data=b"", error="ENOENT")


NOWHERE = Path("/nonexistent/golden-repos") / REPO


def _manifest_for(files: Dict[str, bytes], tmp_path: Path) -> RepoManifest:
    """A manifest of ``files`` taken elsewhere, but rooted at a path that does
    not exist here -- the pod's view."""
    held = tmp_path / "held"
    for rel, data in files.items():
        (held / rel).parent.mkdir(parents=True, exist_ok=True)
        (held / rel).write_bytes(data)
    page = LocalContentBackend().manifest(held, page_size=10_000)

    class Listing(LocalContentBackend):
        @property
        def is_local(self) -> bool:
            return False

        def manifest(self, repo_path: Any, **kw: Any) -> ContentPage:
            return page

    return RepoManifest.build(NOWHERE, backend=Listing())


def _images(n: int) -> Dict[str, bytes]:
    return {f"img/{i:03d}.png": bytes([i % 256]) * (10 + i) for i in range(n)}


@pytest.mark.parametrize("n,page", [(7, 3), (6, 3), (1, 3), (10, 1)])
def test_fetch_calls_are_ceil_n_over_page(n: int, page: int, tmp_path: Path) -> None:
    files = _images(n)
    files["src/app.py"] = b"not an image\n"
    backend = _Recording(files)
    source = ImagePrefetcher(
        backend,
        NOWHERE,
        _manifest_for(files, tmp_path),
        SUPPORTED_MEDIA_TYPES,
        budget_files=page,
    )
    assert not NOWHERE.exists()

    for rel in reversed(sorted(_images(n))):  # any order, and twice each
        assert source.read(NOWHERE / rel) == files[rel]
        assert source.read(NOWHERE / rel) == files[rel]

    assert len(backend.fetches) == math.ceil(n / page)
    # Full pages plus at most one remainder: no image travels alone unless the
    # page or the remainder is one.
    expected = [page] * (n // page) + ([n % page] if n % page else [])
    assert sorted(len(call) for call in backend.fetches) == sorted(expected)
    assert "src/app.py" not in sum(backend.fetches, [])


def test_an_unreferenced_batch_is_never_fetched(tmp_path: Path) -> None:
    files = _images(9)
    backend = _Recording(files)
    source = ImagePrefetcher(
        backend,
        NOWHERE,
        _manifest_for(files, tmp_path),
        SUPPORTED_MEDIA_TYPES,
        budget_files=3,
    )
    source.read(NOWHERE / "img/004.png")
    assert backend.fetches == [["img/003.png", "img/004.png", "img/005.png"]]


def test_existence_and_size_come_from_the_manifest(tmp_path: Path) -> None:
    files = {"img/a.png": b"x" * 123}
    backend = _Recording(files)
    source = ImagePrefetcher(
        backend,
        NOWHERE,
        _manifest_for(files, tmp_path),
        SUPPORTED_MEDIA_TYPES,
    )
    assert source.has(NOWHERE / "img/a.png") is True
    assert source.has(NOWHERE / "img/gone.png") is False
    assert source.size(NOWHERE / "img/a.png") == 123
    assert backend.fetches == [], "membership and size must not fetch bytes"


def test_failures_keep_their_meaning(tmp_path: Path) -> None:
    files = {"img/a.png": b"a", "img/b.png": b"b"}
    manifest = _manifest_for(files, tmp_path)
    vanished = _Recording({"img/a.png": b"a"})  # b went away after the manifest
    source = ImagePrefetcher(vanished, NOWHERE, manifest, SUPPORTED_MEDIA_TYPES)
    with pytest.raises(FileNotFoundError):
        source.read(NOWHERE / "img/b.png")
    with pytest.raises(FileNotFoundError):
        source.read(NOWHERE / "img/never.png")

    down = _Recording(files, fail=True)
    source = ImagePrefetcher(down, NOWHERE, manifest, SUPPORTED_MEDIA_TYPES)
    with pytest.raises(ContentUnavailableError):
        source.read(NOWHERE / "img/a.png")
    with pytest.raises(ContentUnavailableError):
        source.read(NOWHERE / "img/b.png")
    assert len(down.fetches) == 1, "a failed batch is not retried per image"


def test_resident_bytes_are_bounded(tmp_path: Path) -> None:
    files = {f"img/{i}.png": bytes([i]) * 100 for i in range(6)}
    backend = _Recording(files)
    source = ImagePrefetcher(
        backend,
        NOWHERE,
        _manifest_for(files, tmp_path),
        SUPPORTED_MEDIA_TYPES,
        budget_files=2,
        resident_bytes=250,
    )
    for i in range(6):
        source.read(NOWHERE / f"img/{i}.png")
    assert len(backend.fetches) == 3
    # Only the most recent batch fits; the first was dropped and costs a
    # (batched) fetch again.
    source.read(NOWHERE / "img/0.png")
    assert backend.fetches[-1] == ["img/0.png", "img/1.png"]
    assert len(backend.fetches) == 4


def test_cohere_size_limit_reads_the_manifest_size_not_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.services import cohere_multimodal

    source = MagicMock()
    source.size.return_value = cohere_multimodal.COHERE_MAX_IMAGE_SIZE + 1
    client = cohere_multimodal.CohereMultimodalClient.__new__(
        cohere_multimodal.CohereMultimodalClient
    )
    blocks = client._build_content_blocks("t", [NOWHERE / "img/big.png"], source)
    assert blocks == [{"type": "text", "text": "t"}]
    source.read.assert_not_called()

    source.size.return_value = 3
    source.read.return_value = b"abc"
    blocks = client._build_content_blocks("t", [NOWHERE / "img/ok.png"], source)
    assert blocks[1]["image_url"]["url"] == "data:image/png;base64,YWJj"


# ── local is unchanged ──────────────────────────────────────────────────────


def test_local_pass_has_no_image_source_and_calls_the_client_as_before(
    tmp_path: Path,
) -> None:
    from code_indexer.services.file_chunking_manager import FileChunkingManager
    from code_indexer.services.high_throughput_processor import (
        HighThroughputProcessor,
    )

    processor = HighThroughputProcessor.__new__(HighThroughputProcessor)
    processor.repo_manifest = None
    assert processor._build_image_source(MagicMock()) is None

    manager = FileChunkingManager.__new__(FileChunkingManager)
    manager.image_source = None
    assert manager._image_source_kwargs() == {}
    (tmp_path / "a.png").write_bytes(b"a")
    assert manager._image_exists(tmp_path / "a.png") is True
    assert manager._image_exists(tmp_path / "gone.png") is False


def test_a_delegated_pass_without_a_multimodal_client_builds_no_source() -> None:
    from code_indexer.services.high_throughput_processor import (
        HighThroughputProcessor,
    )

    processor = HighThroughputProcessor.__new__(HighThroughputProcessor)
    processor.repo_manifest = MagicMock()
    assert processor._build_image_source(None) is None


def test_local_encoding_reads_the_disk(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_bytes(b"abc")
    assert encode_image_to_base64(tmp_path / "a.png") == "data:image/png;base64,YWJj"
    with pytest.raises(FileNotFoundError, match="Image file not found"):
        encode_image_to_base64(tmp_path / "gone.png")


def test_an_unsupported_format_is_refused_before_any_fetch() -> None:
    source = MagicMock()
    with pytest.raises(ValueError):
        encode_image_to_base64(NOWHERE / "diagram.svg", source)
    source.read.assert_not_called()


def test_local_cli_index_builds_the_same_points_as_before(
    tmp_path: Path, held: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mounted run embeds every reachable image, with no content backend."""
    points, vendor = _local_run(tmp_path, held, monkeypatch)
    assert len(points) == 3
    sent = json.dumps(vendor.requests)
    for rel in (
        "docs/images/a.png",
        "images/b.jpg",
        "assets/c.gif",
        "docs/images/d.webp",
    ):
        assert encode_image_to_base64(held / rel) in sent
    assert encode_image_to_base64(held / "docs/images/unreferenced.png") not in sent
