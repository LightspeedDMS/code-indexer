"""issue-64616: files indexed before every chunk reached the text index heal once.

Under the old routing an image chunk's text never reached the text index, so a
page indexed then is stored without some of its chunks. Reconcile compares
whole files and never revisited it. An index whose metadata does not record
the current chunk coverage version is checked once -- by the next incremental
run or reconcile -- from the stored payloads: only the incomplete files are
re-processed, only their missing chunks are embedded (the stored ones are
reused), and the version is recorded so the next run checks nothing.

Pinned end to end on the delegated pod shape of
``test_reconcile_git_seam_mountless``: real CLI, real store, a deterministic
embedder that records what it is sent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

from code_indexer.services.chunk_coverage import (
    CHUNK_COVERAGE_KEY,
    CHUNK_COVERAGE_VERSION,
)
from tests.unit.services.test_reconcile_git_seam_mountless import (
    _Elsewhere,
    _Embedder,
    _git,
    _git_mcp,
    _WorkspaceMcp,
)

#: More than one chunk.
BIG_FILE = "src/big.py"
SMALL_FILE = "src/small.py"
_SEAMS = ("--content-backend=delegated", "--git-backend=delegated")
_LAYOUT = "--new-collection-layout=chunks_db"


class _RecordingEmbedder(_Embedder):
    """The deterministic embedder, recording every text it is sent."""

    sent: List[str] = []

    def get_embeddings_batch(self, texts: List[str], *a: Any, **kw: Any) -> Any:
        _RecordingEmbedder.sent.extend(texts)
        return super().get_embeddings_batch(texts, *a, **kw)

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], *a: Any, **kw: Any
    ) -> Any:
        _RecordingEmbedder.sent.extend(texts)
        return super().get_embeddings_batch_with_metadata(texts, *a, **kw)

    def get_embedding(self, text: str, *a: Any, **kw: Any) -> Any:
        _RecordingEmbedder.sent.append(text)
        return super().get_embedding(text, *a, **kw)


@pytest.fixture
def pod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    held = tmp_path / "git-mcp" / "docs-repo"
    (held / "src").mkdir(parents=True)
    (held / BIG_FILE).write_text(
        "".join(f"# section {i}: " + "x" * 80 + "\n" for i in range(60))
    )
    (held / SMALL_FILE).write_text("def small():\n    return 1\n")
    _git(held, "init", "-q", "-b", "master")
    _git(held, "add", ".")
    _git(held, "commit", "-qm", "init")
    here = tmp_path / "golden-repos" / "docs-repo"
    here.mkdir(parents=True)
    elsewhere = _Elsewhere(here, held)

    from code_indexer import cli as cli_module
    from code_indexer.services import embedding_factory

    _RecordingEmbedder.sent = []
    monkeypatch.setattr(
        embedding_factory.EmbeddingProviderFactory,
        "create",
        staticmethod(lambda *a, **k: _RecordingEmbedder()),
    )
    monkeypatch.setattr(
        cli_module, "_build_index_git_backend", lambda name: _git_mcp(elsewhere)
    )
    monkeypatch.setattr(
        cli_module,
        "_build_index_content_backend",
        lambda name: _WorkspaceMcp(elsewhere),
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "not-used")
    monkeypatch.chdir(here)
    _run(here, cli_module.cli, "init", "--embedding-provider", "voyage-ai")
    _run(here, cli_module.cli, "index", _LAYOUT, *_SEAMS)
    return {"here": here, "cli": cli_module.cli}


def _run(here: Path, cli: Any, *args: str) -> str:
    result = CliRunner().invoke(cli, list(args), catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return str(result.output)


def _index(pod: Dict[str, Any], *extra: str) -> List[str]:
    """Run ``cidx index`` and return the content hashes of the texts it
    embedded (the payload's ``content_hash``)."""
    _RecordingEmbedder.sent = []
    _run(pod["here"], pod["cli"], "index", _LAYOUT, *_SEAMS, *extra)
    return [hashlib.sha256(t.encode()).hexdigest() for t in _RecordingEmbedder.sent]


def _store(pod: Dict[str, Any]) -> Any:
    from code_indexer.config import ConfigManager
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    config = ConfigManager.create_with_backtrack(pod["here"]).get_config()
    store = FilesystemVectorStore(
        pod["here"] / ".code-indexer" / "index", project_root=pod["here"]
    )
    return store, store.resolve_collection_name(config, _Embedder())


def _chunks(pod: Dict[str, Any], path: str) -> Dict[int, Dict[str, Any]]:
    store, collection = _store(pod)
    points, _ = store.scroll_points(collection, limit=1000, with_payload=True)
    return {
        p["payload"]["chunk_index"]: p for p in points if p["payload"]["path"] == path
    }


def _metadata_path(pod: Dict[str, Any]) -> Path:
    return pod["here"] / ".code-indexer" / "metadata-voyage-ai.json"


def _as_indexed_before_the_fix(pod: Dict[str, Any], drop_chunk: int) -> str:
    """Remove one stored chunk of the big file, as the old routing left an
    image chunk out, and the coverage version an old index never recorded.
    Returns the dropped chunk's content hash."""
    chunks = _chunks(pod, BIG_FILE)
    assert len(chunks) == chunks[0]["payload"]["total_chunks"] > drop_chunk
    dropped = chunks[drop_chunk]
    store, collection = _store(pod)
    store.delete_points(collection, [dropped["id"]])
    _forget_coverage(pod)
    return str(dropped["payload"]["content_hash"])


def _forget_coverage(pod: Dict[str, Any]) -> None:
    data = json.loads(_metadata_path(pod).read_text())
    data.pop(CHUNK_COVERAGE_KEY, None)
    _metadata_path(pod).write_text(json.dumps(data))


def _coverage(pod: Dict[str, Any]) -> Any:
    return json.loads(_metadata_path(pod).read_text()).get(CHUNK_COVERAGE_KEY)


def test_a_fresh_index_is_recorded_as_checked(pod: Dict[str, Any]) -> None:
    assert _coverage(pod) == CHUNK_COVERAGE_VERSION
    assert _index(pod) == []


@pytest.mark.parametrize("extra", [(), ("--reconcile", "--ignore-resume-state")])
def test_only_the_missing_chunk_is_embedded_once(
    pod: Dict[str, Any], extra: tuple
) -> None:
    dropped = _as_indexed_before_the_fix(pod, drop_chunk=1)

    assert _index(pod, *extra) == [dropped]

    chunks = _chunks(pod, BIG_FILE)
    assert sorted(chunks) == list(range(chunks[0]["payload"]["total_chunks"]))
    assert _coverage(pod) == CHUNK_COVERAGE_VERSION
    # Idempotent: the next run checks nothing and embeds nothing.
    assert _index(pod, *extra) == []


def test_a_complete_index_is_recorded_without_embedding(
    pod: Dict[str, Any],
) -> None:
    _forget_coverage(pod)

    assert _index(pod) == []

    assert _coverage(pod) == CHUNK_COVERAGE_VERSION
