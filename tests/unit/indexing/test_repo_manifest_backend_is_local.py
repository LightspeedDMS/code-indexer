"""``RepoManifest.backend_is_local`` is the public answer to "is this disk the
backend's?", read by FileIdentifier instead of a private attribute."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import ContentPage
from code_indexer.services.file_identifier import _is_delegated_manifest


def test_defaults_to_local(tmp_path: Path) -> None:
    assert RepoManifest(tmp_path, {}).backend_is_local is True


def test_reports_a_delegated_backend(tmp_path: Path) -> None:
    assert RepoManifest(tmp_path, {}, backend_is_local=False).backend_is_local is False


def test_is_read_only(tmp_path: Path) -> None:
    manifest = RepoManifest(tmp_path, {})
    with pytest.raises(AttributeError):
        setattr(manifest, "backend_is_local", False)


@pytest.mark.parametrize("is_local", [True, False])
def test_build_takes_it_from_the_backend(tmp_path: Path, is_local: bool) -> None:
    backend = SimpleNamespace(
        is_local=is_local,
        manifest=lambda root, cursor=None, page_size=1000: ContentPage(
            entries=(), next_cursor=None
        ),
    )
    assert RepoManifest.build(tmp_path, backend).backend_is_local is is_local


def test_file_identifier_reads_the_property(tmp_path: Path) -> None:
    assert _is_delegated_manifest(None) is False
    assert _is_delegated_manifest(RepoManifest(tmp_path, {})) is False
    assert (
        _is_delegated_manifest(RepoManifest(tmp_path, {}, backend_is_local=False))
        is True
    )
