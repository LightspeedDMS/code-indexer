from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError
from tests.unit.global_repos.test_local_git_source_removal_2194 import (
    _FAKE_KEY,
    _KEPT,
    _indexed_git_repo,
)
from tests.unit.services.test_git_aware_watch_handler_retry import (
    _content_points,
    _hidden,
    _index_dir,
    _reader,
)
from tests.utils.cidx_fake_provider_repo import COLLECTION
from tests.utils.fake_voyage_provider import running_fake_voyage_server

pytestmark = pytest.mark.slow


class _SessionFailsAfterUpdate(FilesystemVectorStore):
    """A real store whose second HNSW session lookup (the visibility diff,
    after the chunks.db update) fails."""

    session_calls = 0

    def _get_or_create_hnsw_sync_session(self, *args: Any, **kwargs: Any) -> Any:
        self.session_calls += 1
        if self.session_calls == 2:
            raise OSError("hnsw session bookkeeping failed")
        return super()._get_or_create_hnsw_sync_session(*args, **kwargs)


def test_hnsw_session_failure_after_the_update_is_not_a_chunk_store_failure(
    tmp_path: Path,
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, False)
        kept = f"src/{_KEPT}.py"
        store = _SessionFailsAfterUpdate(
            base_path=_index_dir(repo), project_root=repo.repo
        )
        updates = [
            {"id": p["id"], "payload": {"hidden_branches": ["feature"]}}
            for p in _content_points(store, kept)
        ]

        with pytest.raises(OSError) as raised:
            store._batch_update_payload_only(updates, COLLECTION)

        assert not isinstance(raised.value, ChunkStoreUnavailableError)
        assert "feature" in _hidden(_reader(repo), kept)
