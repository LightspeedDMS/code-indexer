"""Bug #1991: query-time content retrieval for non-UTF-8 source files.

Real files on disk, a real git repo, the real FixedSizeChunker and the real
FilesystemVectorStore. Only the embedding provider (an external service) is
replaced by a stub returning random vectors -- every chunk is returned by the
search because ``limit`` exceeds the chunk count.

The oracle for "what retrieval must return" is the indexer's own text-mode
read (``open(path, encoding=...)``, universal newlines, ``readlines()``),
never the helper under test.
"""

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock

import numpy as np
import pytest

from code_indexer.config import IndexingConfig
from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

COLLECTION = "c1991"
DIM = 64


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "core.autocrlf", "false")


def _commit_all(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")


def _latin1_source() -> bytes:
    lines = ["/// Caf\xe9 image generator © na\xefve r\xe9sum\xe9"]
    for i in range(120):
        lines.append(f'    void Gen{i}() {{ var s = "cr\xe8me br\xfbl\xe9e {i}"; }}')
    return ("\n".join(lines) + "\n").encode("latin-1")


def _index_file(repo: Path, rel: str) -> FilesystemVectorStore:
    """Chunk ``rel`` with the real chunker and upsert into a real store."""
    chunker = FixedSizeChunker(IndexingConfig())
    chunks = chunker.chunk_file(repo / rel, repo_root=repo)
    assert len(chunks) > 2, "fixture must span several chunks"
    store = FilesystemVectorStore(base_path=repo / ".idx", project_root=repo)
    store.create_collection(COLLECTION, vector_size=DIM)
    points = [
        {
            "id": f"p{i}",
            "vector": np.random.randn(DIM).tolist(),
            "payload": {
                "path": rel,
                "line_start": chunk["line_start"],
                "line_end": chunk["line_end"],
                "content": chunk["text"],
                "type": "content",
            },
        }
        for i, chunk in enumerate(chunks)
    ]
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, points)
    store.end_indexing(COLLECTION)
    store._test_chunks = {  # type: ignore[attr-defined]
        (c["line_start"], c["line_end"]): c["text"] for c in chunks
    }
    return store


def _search(store: FilesystemVectorStore) -> List[Dict[str, Any]]:
    provider = Mock()
    provider.get_embedding.return_value = np.random.randn(DIM).tolist()
    results = store.search(
        query="image generator",
        embedding_provider=provider,
        collection_name=COLLECTION,
        limit=500,
    )
    assert isinstance(results, list)
    return results


def _oracle_lines(path: Path, encoding: str) -> List[str]:
    with open(path, encoding=encoding) as f:
        return f.readlines()


def _assert_hits_match(
    store: FilesystemVectorStore, oracle: List[str], expect_stale: bool
) -> None:
    results = _search(store)
    chunk_texts = store._test_chunks  # type: ignore[attr-defined]
    assert len(results) == len(chunk_texts)
    for result in results:
        payload = result["payload"]
        start, end = payload["line_start"], payload["line_end"]
        content = payload["content"]
        assert "codec" not in content
        assert content == "".join(oracle[start - 1 : end])
        assert chunk_texts[(start, end)] in content
        assert result["staleness"]["is_stale"] is expect_stale


def test_latin1_working_tree_tier_returns_decoded_lines(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "ImageGenerator.cs").write_bytes(_latin1_source())
    _commit_all(tmp_path)
    store = _index_file(tmp_path, "ImageGenerator.cs")

    oracle = _oracle_lines(tmp_path / "ImageGenerator.cs", "latin-1")
    _assert_hits_match(store, oracle, expect_stale=False)


def test_latin1_git_blob_tier_deleted_file_returns_decoded_lines(tmp_path):
    _init_repo(tmp_path)
    source = tmp_path / "ImageGenerator.cs"
    source.write_bytes(_latin1_source())
    _commit_all(tmp_path)
    store = _index_file(tmp_path, "ImageGenerator.cs")
    oracle = _oracle_lines(source, "latin-1")

    source.unlink()  # forces the git-blob tier

    _assert_hits_match(store, oracle, expect_stale=True)
    for result in _search(store):
        assert result["staleness"]["staleness_reason"] == "file_deleted"


def _cp1252_nel_crlf_source() -> bytes:
    # 0x85 is CP1252's ellipsis; decoded as latin-1 it becomes U+0085 (NEL),
    # which str.splitlines() treats as a line break. CRLF and a lone CR must
    # be normalised exactly like the chunker's text-mode read.
    lines = [b"// Caf\xe9 header\x85 done"]
    for i in range(100):
        lines.append(b"    int v%d = %d; // more\x85 text \xe8" % (i, i))
    return b"\r\n".join(lines) + b"\rtail line\r\n"


def _utf8_separator_source() -> bytes:
    nel, line_sep, form_feed, vtab = chr(0x85), chr(0x2028), chr(0x0C), chr(0x0B)
    lines = ["# caf" + chr(0xE9) + " " + nel + " header"]
    for i in range(100):
        lines.append(f"x{i} = {i}  # page{form_feed}break {line_sep} sep {vtab} vt")
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.mark.parametrize(
    "source_factory, encoding",
    [(_cp1252_nel_crlf_source, "latin-1"), (_utf8_separator_source, "utf-8")],
    ids=["cp1252-nel-crlf", "utf8-separators"],
)
@pytest.mark.parametrize("tier", ["working_tree", "git_blob"])
def test_line_alignment_preserved(tmp_path, source_factory, encoding, tier):
    _init_repo(tmp_path)
    source = tmp_path / "Legacy.cs"
    source.write_bytes(source_factory())
    _commit_all(tmp_path)
    store = _index_file(tmp_path, "Legacy.cs")
    oracle = _oracle_lines(source, encoding)

    if tier == "git_blob":
        # Modified after indexing -> hash mismatch -> git-blob tier.
        source.write_bytes(b"replaced\n" + source_factory())

    _assert_hits_match(store, oracle, expect_stale=(tier == "git_blob"))


def _remove_blob_object(repo: Path, rel: str) -> None:
    blob = subprocess.run(
        ["git", "rev-parse", f"HEAD:{rel}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()


@pytest.mark.parametrize(
    "breakage, indicator, reason",
    [
        ("unreadable", "❌ Error", "retrieval_failed"),
        ("deleted", "🗑️ Deleted", "file_deleted"),
    ],
)
def test_unreadable_chunk_reports_unavailable_not_exception_text(
    tmp_path, breakage, indicator, reason
):
    _init_repo(tmp_path)
    source = tmp_path / "ImageGenerator.cs"
    source.write_bytes(_latin1_source())
    _commit_all(tmp_path)
    store = _index_file(tmp_path, "ImageGenerator.cs")

    _remove_blob_object(tmp_path, "ImageGenerator.cs")
    source.unlink()
    if breakage == "unreadable":
        source.mkdir()  # exists, but reading it raises IsADirectoryError

    results = _search(store)
    assert results
    for result in results:
        assert result["payload"]["content"] == ""
        staleness = result["staleness"]
        assert staleness["content_unavailable"] is True
        assert staleness["is_stale"] is True
        assert staleness["staleness_indicator"] == indicator
        assert staleness["staleness_reason"] == reason


_UNAVAILABLE_LOG = "Chunk content unavailable"


def _unavailable_records(caplog, level):
    return [
        r
        for r in caplog.records
        if r.levelno == level and _UNAVAILABLE_LOG in r.getMessage()
    ]


def test_unavailable_chunk_warns_once_per_file(tmp_path, caplog):
    _init_repo(tmp_path)
    source = tmp_path / "ImageGenerator.cs"
    source.write_bytes(_latin1_source())
    _commit_all(tmp_path)
    store = _index_file(tmp_path, "ImageGenerator.cs")  # several chunks
    _remove_blob_object(tmp_path, "ImageGenerator.cs")
    source.unlink()
    source.mkdir()

    caplog.set_level(logging.DEBUG, logger="code_indexer.storage")
    _search(store)
    assert len(_unavailable_records(caplog, logging.WARNING)) == 1

    caplog.clear()
    _search(store)
    assert _unavailable_records(caplog, logging.WARNING) == []
    assert _unavailable_records(caplog, logging.DEBUG)


def test_unavailable_warning_set_is_bounded(monkeypatch):
    import code_indexer.storage.filesystem_vector_store as fvs

    monkeypatch.setattr(fvs, "_UNAVAILABLE_WARNED_MAX", 1)
    key_a, key_b = ("/repo-a", "a.cs"), ("/repo-b", "b.cs")

    assert fvs._first_unavailable_report(key_a) is True
    assert fvs._first_unavailable_report(key_a) is False
    assert fvs._first_unavailable_report(key_b) is True
    # key_a was evicted to stay within the bound, so it reports again.
    assert fvs._first_unavailable_report(key_a) is True
