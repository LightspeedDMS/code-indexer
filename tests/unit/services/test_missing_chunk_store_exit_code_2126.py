"""Story #2126: the missing store of an ESTABLISHED collection is its own
fatal chunk-store failure kind, never relabelled as corruption (86) and no
longer lumped with every other environment failure (87).

The error is produced by the real guard (``refuse_missing_established_store``
over a real CHUNKS_DB collection whose ``chunks.db`` is gone), then wrapped
exactly as the reuse lookup and the per-file writer wrap it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterator

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo

from code_indexer.services.index_failure_exit_codes import (
    EXIT_CODE_CHUNK_STORE_CORRUPTION,
    EXIT_CODE_CHUNK_STORE_ENVIRONMENT,
    EXIT_CODE_RUN_SPEND_DEFECT,
    chunk_store_failure_kind_for_exit_code,
    classify_fatal_chunk_store_failure,
    index_failure_exit_code,
)
from code_indexer.storage.sqlite_chunk_store import (
    ChunkStoreUnavailableError,
    refuse_missing_established_store,
)

#: The next free reserved code after 86 (corruption), 87 (environment) and
#: 88 (run-spend defect).
EXPECTED_MISSING_STORE_EXIT_CODE = 89


def _established_collection_without_store(tmp_path: Path) -> Path:
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    index_dir = tmp_path / "index"
    store = FilesystemVectorStore(
        base_path=index_dir,
        project_root=tmp_path,
        use_chunks_db_for_new_collections=True,
    )
    assert store.create_collection("example-collection", 8)
    store.begin_indexing("example-collection")
    store.upsert_points(
        "example-collection",
        [{"id": "p-1", "vector": [0.1] * 8, "payload": {"path": "a.py"}}],
    )
    store.end_indexing("example-collection")
    chunks_db = index_dir / "example-collection" / "chunks.db"
    assert chunks_db.exists()
    chunks_db.unlink()
    return chunks_db


def _refusal(tmp_path: Path) -> ChunkStoreUnavailableError:
    chunks_db = _established_collection_without_store(tmp_path)
    with pytest.raises(ChunkStoreUnavailableError) as raised:
        refuse_missing_established_store(chunks_db)
    return raised.value


def _wrapped_like_the_reuse_lookup(inner: BaseException) -> BaseException:
    try:
        try:
            raise inner
        except ChunkStoreUnavailableError as caught:
            raise ChunkStoreUnavailableError(
                f"Embedding reuse lookup failed for a.py; aborting: {caught}"
            ) from caught
    except ChunkStoreUnavailableError as wrapped:
        return wrapped


def test_missing_established_store_is_its_own_kind(tmp_path: Path) -> None:
    refusal = _refusal(tmp_path)

    kind = classify_fatal_chunk_store_failure(refusal)

    assert kind is not None and kind.value == "missing_store", kind


def test_missing_store_exit_code_is_reserved_and_distinct(tmp_path: Path) -> None:
    refusal = _refusal(tmp_path)

    code = index_failure_exit_code(refusal)

    assert code == EXPECTED_MISSING_STORE_EXIT_CODE
    assert code not in (
        EXIT_CODE_CHUNK_STORE_CORRUPTION,
        EXIT_CODE_CHUNK_STORE_ENVIRONMENT,
        EXIT_CODE_RUN_SPEND_DEFECT,
    )
    parent_kind = chunk_store_failure_kind_for_exit_code(code)
    assert parent_kind is not None and parent_kind.value == "missing_store"


def test_missing_store_survives_the_reuse_lookup_wrapping(tmp_path: Path) -> None:
    wrapped = _wrapped_like_the_reuse_lookup(_refusal(tmp_path))

    assert index_failure_exit_code(wrapped) == EXPECTED_MISSING_STORE_EXIT_CODE


_FAKE_KEY = "issue-2126-fake-key"


@pytest.fixture
def fake_provider() -> Iterator[object]:
    from tests.utils.fake_voyage_provider import running_fake_voyage_server

    with running_fake_voyage_server(_FAKE_KEY) as server:
        yield server


def _drop_metadata(repo: FakeProviderRepo) -> None:
    repo.metadata_path.unlink()


def _drop_resume_watermark(repo: FakeProviderRepo) -> None:
    metadata = repo.metadata()
    metadata["last_index_timestamp"] = 0.0
    repo.metadata_path.write_text(json.dumps(metadata))


@pytest.mark.slow
@pytest.mark.timeout(240)
@pytest.mark.parametrize(
    "disturb",
    [_drop_metadata, _drop_resume_watermark],
    ids=["metadata-missing", "no-resume-watermark"],
)
def test_implicit_full_index_routes_never_rebuild_a_missing_store(
    tmp_path: Path, fake_provider, disturb: Callable[[FakeProviderRepo], None]
) -> None:
    """A run whose metadata no longer matches (missing metadata, no resume
    watermark) asks whether the collection is populated before choosing the
    implicit full index; a missing established store is not "empty" -- the
    run refuses (89) instead of wiping and paying for a full rebuild."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _FAKE_KEY)
    repo.write_files("base", 3)
    proc = repo.cidx(*repo.index_args())
    assert proc.returncode == 0, proc.stderr[-3000:]
    repo.chunks_db.unlink()
    disturb(repo)

    fake_provider.ledger.begin_run("disturbed")  # type: ignore[attr-defined]
    proc = repo.cidx(*repo.index_args())

    inputs = fake_provider.ledger.run_stats("disturbed")["inputs"]  # type: ignore[attr-defined]
    assert inputs == 0, "a missing store was rebuilt and the provider paid"
    assert proc.returncode == EXPECTED_MISSING_STORE_EXIT_CODE, (
        proc.returncode,
        proc.stderr[-3000:],
    )
    assert not repo.chunks_db.exists(), "the missing store was recreated"


def test_other_file_not_found_stays_environment(tmp_path: Path) -> None:
    """Only the guard's refusal is a missing store: any other typed failure
    caused by a missing file (e.g. a vanished directory) stays 87."""
    try:
        try:
            raise FileNotFoundError(2, "No such file or directory", "x")
        except FileNotFoundError as inner:
            raise ChunkStoreUnavailableError("unavailable") from inner
    except ChunkStoreUnavailableError as typed:
        assert index_failure_exit_code(typed) == EXIT_CODE_CHUNK_STORE_ENVIRONMENT
