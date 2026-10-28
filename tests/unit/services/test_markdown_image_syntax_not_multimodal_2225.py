"""Bug #2225: markdown image syntax with no real in-repository image behind
it is plain text through the whole indexing path.

Drives the REAL ``cidx`` CLI (subprocess, the tree under test) in a temporary
non-git repository against the local fake VoyageAI server under the network
guard of tests/utils/cidx_fake_provider_repo.py (no real provider call is
possible); the multimodal client is re-pointed at the fake exactly as the
Bug #2153 tests do."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from tests.unit.services.test_multimodal_embedding_reuse_2153 import (
    _route_multimodal_to,
)
from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SENTINEL_KEY = "issue-2225-fake-key"
_MULTIMODAL_COLLECTION = "voyage-multimodal-3"
_DOC = "docs/writing-guide.md"

pytestmark = [pytest.mark.slow, pytest.mark.timeout(240)]


@pytest.fixture
def fake_provider() -> Iterator[Any]:
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


def _repo(root: Path, server: Any) -> FakeProviderRepo:
    repo = FakeProviderRepo(
        root,
        server,
        _SENTINEL_KEY,
        extra_sitecustomize=_route_multimodal_to(server.base_url),
    )
    (repo.repo / "docs").mkdir()
    return repo


def _multimodal_dir(repo: FakeProviderRepo) -> Path:
    return repo.repo / ".code-indexer" / "index" / _MULTIMODAL_COLLECTION


def test_image_syntax_without_a_real_image_indexes_as_plain_text(
    tmp_path: Path, fake_provider: Any
) -> None:
    """The incident: the only file of the run explains the syntax in prose
    and references a missing image. It must index as text, not fail, and
    leave no multimodal collection behind."""
    repo = _repo(tmp_path, fake_provider)
    (repo.repo / _DOC).write_text(
        "# Writing guide\n\n"
        "Embed a picture using `![](path)` syntax in any example document.\n\n"
        "![architecture diagram](missing-diagram.png)\n"
    )

    fake_provider.ledger.begin_run("index")
    proc = repo.cidx(*repo.index_args())

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert repo.metadata().get("failed_file_paths") == []
    assert not _multimodal_dir(repo).exists()
    assert fake_provider.ledger.run_multimodal_key_counts("index") == {}
    assert _DOC in set(repo.stored_rows().values())


def test_real_in_repository_png_still_goes_multimodal(
    tmp_path: Path, fake_provider: Any
) -> None:
    repo = _repo(tmp_path, fake_provider)
    (repo.repo / "docs" / "diagram.png").write_bytes(
        b"\x89PNG\r\n\x1a\nexample-image-2225"
    )
    (repo.repo / _DOC).write_text(
        "# Writing guide\n\nThe example service flow.\n\n![diagram](diagram.png)\n"
    )

    fake_provider.ledger.begin_run("index")
    proc = repo.cidx(*repo.index_args())

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert repo.metadata().get("failed_file_paths") == []
    assert (_multimodal_dir(repo) / "chunks.db").is_file()
    sent = fake_provider.ledger.run_multimodal_key_counts("index")
    assert sum(sent.values()) == 1, sent
