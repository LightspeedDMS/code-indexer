"""The fake workspace-mcp's clone client passes the clone suite, and answers
with the remote service's shapes."""

from __future__ import annotations

from pathlib import Path

import pytest

from cidx_sdk.seams.clone import CloneDestinationExistsError
from cidx_testkit.conformance.clone import (
    CloneBackendContract,
    CloneSubject,
    local_subject,
)
from cidx_testkit.fakes.workspace_mcp import (
    FakeWorkspaceMcp,
    FakeWorkspaceMcpCloneBackend,
    FakeWorkspaceMcpHttp,
)


class TestFakeWorkspaceMcpCloneBackend(CloneBackendContract):
    @pytest.fixture
    def clone_subject(self, tmp_path: Path) -> CloneSubject:
        base = tmp_path / "golden"
        base.mkdir()
        service = FakeWorkspaceMcp(store=tmp_path / "store")
        return local_subject(FakeWorkspaceMcpCloneBackend(service, str(base)), base)


def test_every_clone_operation_is_a_service_request(tmp_path: Path) -> None:
    service = FakeWorkspaceMcp(store=tmp_path / "store")
    source = tmp_path / "src"
    source.mkdir()
    backend = FakeWorkspaceMcpCloneBackend(service, str(tmp_path))
    dest = str(tmp_path / "dest")
    backend.create_clone_at_path(str(source), dest)
    with pytest.raises(CloneDestinationExistsError):
        backend.create_clone_at_path(str(source), dest)
    backend.delete_clone(dest)
    routes = [route for route, _ in service.requests]
    assert routes == ["/clone-path", "/clone-path", "/clone-path/delete"]


def test_the_http_double_answers_like_the_service(tmp_path: Path) -> None:
    service = FakeWorkspaceMcp(store=tmp_path / "store")
    http = FakeWorkspaceMcpHttp(service)
    source = tmp_path / "src"
    source.mkdir()
    dest = str(tmp_path / "dest")
    base = "http://workspace-mcp.example.com"
    made = http.post(
        f"{base}/clone-path", json={"source_path": str(source), "dest_path": dest}
    )
    assert made.status_code == 200 and made.json()["dest_path"] == dest
    assert (
        http.post(
            f"{base}/clone-path", json={"source_path": str(source), "dest_path": dest}
        ).status_code
        == 409
    )
    assert http.post(f"{base}/clone-path/stat", json={"path": dest}).json() == {
        "exists": True,
        "is_dir": True,
    }
    assert http.post(f"{base}/clone-path/delete", json={"path": dest}).json() == {
        "deleted": True
    }
    gone = http.post(f"{base}/clone-path/delete", json={"path": dest})
    assert gone.status_code == 404 and "ENOENT" in gone.text
    assert http.post(f"{base}/clone-path/nope", json={}).status_code == 404
