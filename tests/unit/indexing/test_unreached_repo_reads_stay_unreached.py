"""Repository reads the content-seam inventory classifies as never reached by a
delegated server, pinned by the guard each classification names.

If one of these fails, the access became reachable: migrate it onto the seam
(or refuse it) and reclassify its row, rather than editing the expectation.
"""

from __future__ import annotations

import ast
import builtins
import functools
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Set
from unittest.mock import MagicMock

import pytest

from code_indexer.config import Config
from code_indexer.indexing.file_finder import FileFinder
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

_SRC = Path(__file__).resolve().parents[3] / "src" / "code_indexer"


@functools.lru_cache(maxsize=1)
def _call_sites() -> Dict[str, Set[str]]:
    """Called name -> modules (relative to src/code_indexer) calling it as
    ``name(...)`` or ``x.name(...)``. Parsed once for every check."""
    sites: Dict[str, Set[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    sites.setdefault(func.attr, set()).add(rel)
                elif isinstance(func, ast.Name):
                    sites.setdefault(func.id, set()).add(rel)
    return sites


@pytest.mark.parametrize(
    "attr,allowed",
    [
        # TextChunker is exported but never instantiated: the indexing
        # pipeline chunks with FixedSizeChunker, whose image extraction never
        # validates on disk (FileChunkingManager._image_exists asks the
        # image_source instead).
        ("TextChunker", set()),
        ("validate_image", {"indexing/chunker.py"}),
        ("validate_image_with_reason", {"indexing/image_extractor.py"}),
        ("extract_images_with_validation", {"indexing/chunker.py"}),
        # X-Ray: test-only single-file API and an unused parser entry point.
        ("_evaluate_file", set()),
        ("parse_file", set()),
        # Watch mode only; the server's AutoWatchManager refuses to start a
        # watch under a delegated content backend.
        ("process_files_incrementally", {"services/git_aware_watch_handler.py"}),
        # `cidx query` only, which the server spawns for composite (activated)
        # repositories alone.
        ("GenericQueryService", {"cli.py"}),
    ],
)
def test_no_production_caller_reaches_it(attr: str, allowed: set) -> None:
    assert _call_sites().get(attr, set()) <= allowed


def test_the_text_sniff_never_opens_a_file(tmp_path: Path, monkeypatch: Any) -> None:
    """_get_base_filtering_result returns before _is_text_file unless the
    extension is configured, and _is_text_file returns True on that same
    check before its open()."""
    finder = FileFinder(Config(codebase_dir=tmp_path, file_extensions=["py"]))
    monkeypatch.setattr(builtins, "open", lambda *a, **k: pytest.fail(f"opened {a[0]}"))
    assert finder._get_base_filtering_result(tmp_path / "a.py") is True
    assert finder._get_base_filtering_result(tmp_path / "README") is False
    assert finder._get_base_filtering_result(tmp_path / "x.bin") is False


def test_a_confined_resume_never_follows_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every server-spawned `cidx index` is confined (--ignore-resume-state);
    a candidate outside the root is rejected before is_symlink()."""
    indexer: Any = SmartIndexer.__new__(SmartIndexer)
    indexer.config = SimpleNamespace(
        codebase_dir=tmp_path / "repo", confined_to_codebase_root=True
    )
    candidate = MagicMock(spec=Path)
    candidate.is_symlink.side_effect = AssertionError("is_symlink() reached")
    import code_indexer.services.smart_indexer as module

    monkeypatch.setattr(module, "resolve_if_within_root", lambda *a: None)
    assert not indexer._resume_candidate_is_safe(candidate, tmp_path / "repo")


@pytest.mark.parametrize(
    "vector_data",
    [
        {"chunk_text": "body", "payload": {"path": "a.jsonl"}},
        {"payload": {"path": "a.jsonl", "content": "body"}},
    ],
)
def test_a_non_git_chunk_never_asks_the_content_seam(
    vector_data: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pod-local local:// repositories (langfuse trace folders) are not git
    repositories: their chunks carry their text and are answered before the
    delegated content-backend branch, so they are never sent to a backend that
    does not hold them."""
    store = FilesystemVectorStore.__new__(FilesystemVectorStore)
    monkeypatch.setattr(
        store, "_content_backend_source", lambda: pytest.fail("asked the seam")
    )
    content, staleness = store._get_chunk_content_with_staleness(vector_data)
    assert content == "body" and staleness["is_stale"] is False
