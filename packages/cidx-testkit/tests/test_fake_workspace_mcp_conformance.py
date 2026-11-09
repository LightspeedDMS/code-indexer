"""The fake workspace-mcp's content client passes the content suite, and
answers with the remote service's shapes."""

from __future__ import annotations

from pathlib import Path

import pytest

from cidx_sdk.seams.content import is_absent_root_error
from cidx_sdk.seams.content_writer import ECONFLICT
from cidx_testkit.conformance.content import (
    ContentBackendContract,
    ContentSubject,
    directory_subject,
)
from cidx_testkit.conformance.content_writer import (
    ContentWriterContract,
    WriterSubject,
)
from cidx_testkit.conformance.ripgrep import RipgrepBackendContract, RipgrepSubject
from cidx_testkit.fakes.workspace_fake import (
    FakeIndexArtifacts,
    FakeServiceError,
    FakeWorkspaceMcpRipgrepBackend,
    FakeWorkspaceMcp,
    FakeWorkspaceMcpContentBackend,
    FakeWorkspaceMcpContentWriter,
    FakeWriteError,
)

_CIDX_ROOT = "/srv/example/golden-repos/example-repo"


class TestFakeWorkspaceMcpContentBackend(ContentBackendContract):
    @pytest.fixture
    def content_subject(self, tmp_path: Path) -> ContentSubject:
        service = FakeWorkspaceMcp(store=tmp_path / "store")
        directory = service.serve(_CIDX_ROOT)
        return directory_subject(
            FakeWorkspaceMcpContentBackend(service), directory, root=_CIDX_ROOT
        )


def test_an_absent_root_is_the_400_answer(tmp_path: Path) -> None:
    backend = FakeWorkspaceMcpContentBackend(FakeWorkspaceMcp(store=tmp_path))
    with pytest.raises(Exception) as caught:
        backend.manifest("/srv/example/nowhere")
    assert is_absent_root_error(caught.value)


def test_requests_are_recorded(tmp_path: Path) -> None:
    service = FakeWorkspaceMcp(store=tmp_path / "store")
    (service.serve(_CIDX_ROOT) / "a.txt").write_bytes(b"a")
    list(FakeWorkspaceMcpContentBackend(service).fetch(_CIDX_ROOT, ["a.txt"]))
    assert service.requests[-1] == (
        "/content/fetch",
        {"source_path": _CIDX_ROOT, "paths": ["a.txt"]},
    )


def test_writes_are_compare_and_swap(tmp_path: Path) -> None:
    service = FakeWorkspaceMcp(store=tmp_path / "store")
    directory = service.serve(_CIDX_ROOT)
    writer = FakeWorkspaceMcpContentWriter(service)
    path = f"{_CIDX_ROOT}/notes.md"
    sha = writer.put(path, b"one", expected_blob_sha=None)
    assert (directory / "notes.md").read_bytes() == b"one"
    with pytest.raises(FakeWriteError) as caught:
        writer.put(path, b"two", expected_blob_sha=None)
    assert caught.value.code == ECONFLICT
    writer.put(path, b"two", expected_blob_sha=sha)
    assert writer.delete(path) is True
    assert writer.delete(path) is False


class TestFakeWorkspaceMcpContentWriter(ContentWriterContract):
    @pytest.fixture
    def writer_subject(self, tmp_path: Path) -> WriterSubject:
        service = FakeWorkspaceMcp(store=tmp_path / "store")
        directory = service.serve(_CIDX_ROOT)
        return WriterSubject(
            FakeWorkspaceMcpContentWriter(service), _CIDX_ROOT, directory
        )


class TestFakeWorkspaceMcpRipgrep(RipgrepBackendContract):
    @pytest.fixture
    def ripgrep_subject(self, tmp_path: Path) -> RipgrepSubject:
        service = FakeWorkspaceMcp(store=tmp_path / "store")
        directory = service.serve(_CIDX_ROOT)
        return RipgrepSubject(
            FakeWorkspaceMcpRipgrepBackend(service), _CIDX_ROOT, directory
        )


def test_index_artifacts_publish_on_a_base_and_check_out_versions(
    tmp_path: Path,
) -> None:
    service = FakeWorkspaceMcp(store=tmp_path / "store")
    artifacts = FakeIndexArtifacts(service)
    built = tmp_path / "built"
    built.mkdir()
    (built / "index.bin").write_bytes(b"v1")
    assert artifacts.current_version("example-repo") is None
    assert artifacts.publish("example-repo", None, str(built)) == "v1"
    (built / "index.bin").write_bytes(b"v2")
    with pytest.raises(FakeServiceError) as caught:
        artifacts.publish("example-repo", None, str(built))
    assert caught.value.status == 409
    assert artifacts.publish("example-repo", "v1", str(built)) == "v2"
    into = tmp_path / "into"
    assert artifacts.checkout("example-repo", str(into), "v1") == "v1"
    assert (into / "index.bin").read_bytes() == b"v1"
    with pytest.raises(FakeServiceError):
        artifacts.checkout("example-repo", str(into), "v2")
    assert artifacts.delete_alias("example-repo") == 2
    assert artifacts.delete_alias("example-repo") == 0
