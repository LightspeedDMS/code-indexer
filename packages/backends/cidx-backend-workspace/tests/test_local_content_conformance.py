"""The local content backend passes the content seam's conformance suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from cidx_backend_workspace.content import LocalContentBackend, make_content_backend
from cidx_testkit.conformance.content import (
    ContentBackendContract,
    ContentSubject,
    directory_subject,
)


class TestLocalContentBackend(ContentBackendContract):
    @pytest.fixture
    def content_subject(self, tmp_path: Path) -> ContentSubject:
        return directory_subject(make_content_backend(), tmp_path / "repo")


def test_the_entry_point_factory_builds_a_local_backend() -> None:
    backend = make_content_backend()
    assert isinstance(backend, LocalContentBackend)
    assert backend.is_local is True
