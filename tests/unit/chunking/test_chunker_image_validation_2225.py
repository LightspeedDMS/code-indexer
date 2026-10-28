"""Bug #2225: a chunk's image refs name only existing, regular, supported
raster files inside the repository root, with one rule for both chunkers.

Image syntax in prose (``![](path)``) or a reference to a missing, foreign
or escaping file is plain text: it must never route a chunk to the
multimodal path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from code_indexer.config import IndexingConfig
from code_indexer.indexing.chunker import TextChunker
from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker

PNG_BYTES = b"\x89PNG\r\n\x1a\nexample-image"

Chunker = Callable[[IndexingConfig], Any]
CHUNKERS: Dict[str, Chunker] = {
    "fixed_size": FixedSizeChunker,
    "text": TextChunker,
}


def _image_refs(chunker_name: str, text: str, file_path: Path, root: Path) -> List[str]:
    chunker = CHUNKERS[chunker_name](IndexingConfig(chunk_size=1000, chunk_overlap=0))
    chunks = chunker.chunk_text(text, file_path, root)
    assert chunks, "the document must produce at least one chunk"
    refs: List[str] = []
    for chunk in chunks:
        for image in chunk["images"]:
            refs.append(image["path"] if isinstance(image, dict) else image)
    return refs


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "example-repo"
    (root / "docs").mkdir(parents=True)
    (tmp_path / "outside.png").write_bytes(PNG_BYTES)
    return root


@pytest.fixture(params=sorted(CHUNKERS))
def chunker_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_image_syntax_in_prose_is_not_an_image(repo: Path, chunker_name: str) -> None:
    text = "# Writing guide\n\nEmbed a picture using `![](path)` syntax.\n"
    refs = _image_refs(chunker_name, text, repo / "docs" / "guide.md", repo)
    assert refs == []


def test_reference_to_missing_image_is_not_an_image(
    repo: Path, chunker_name: str
) -> None:
    text = "# Guide\n\n![diagram](missing.png)\n"
    refs = _image_refs(chunker_name, text, repo / "docs" / "guide.md", repo)
    assert refs == []


def test_html_reference_to_missing_image_is_not_an_image(
    repo: Path, chunker_name: str
) -> None:
    text = '<html><body><img src="missing.png" alt="x"></body></html>\n'
    refs = _image_refs(chunker_name, text, repo / "docs" / "page.html", repo)
    assert refs == []


def test_unsupported_format_is_not_an_image(repo: Path, chunker_name: str) -> None:
    (repo / "docs" / "notes.txt").write_text("plain text")
    (repo / "docs" / "assets").mkdir()
    text = "# Guide\n\n![notes](notes.txt)\n\n![dir](assets)\n"
    refs = _image_refs(chunker_name, text, repo / "docs" / "guide.md", repo)
    assert refs == []


def test_real_in_repository_png_keeps_its_image_ref(
    repo: Path, chunker_name: str
) -> None:
    (repo / "docs" / "diagram.png").write_bytes(PNG_BYTES)
    text = "# Guide\n\n![diagram](diagram.png)\n"
    refs = _image_refs(chunker_name, text, repo / "docs" / "guide.md", repo)
    assert refs == ["docs/diagram.png"]


def test_root_anchored_in_repository_png_keeps_its_image_ref(
    repo: Path, chunker_name: str
) -> None:
    (repo / "docs" / "diagram.png").write_bytes(PNG_BYTES)
    text = "# Guide\n\n![diagram](/docs/diagram.png)\n"
    refs = _image_refs(chunker_name, text, repo / "docs" / "guide.md", repo)
    assert refs == ["docs/diagram.png"]


@pytest.mark.parametrize(
    "reference",
    [
        "../outside.png",
        "/../outside.png",
        "link.png",
        "/link.png",
        "linked-dir/outside.png",
        "/linked-dir/outside.png",
    ],
)
def test_reference_escaping_the_repository_is_not_an_image(
    repo: Path, chunker_name: str, reference: str
) -> None:
    outside = repo.parent / "outside.png"
    os.symlink(outside, repo / "link.png")
    os.symlink(repo.parent, repo / "linked-dir")
    text = f"# Guide\n\n![escape]({reference})\n"
    refs = _image_refs(chunker_name, text, repo / "guide.md", repo)
    assert refs == []


def test_filesystem_absolute_path_is_not_an_image(
    repo: Path, chunker_name: str
) -> None:
    outside = repo.parent / "outside.png"
    text = f"# Guide\n\n![escape]({outside})\n"
    refs = _image_refs(chunker_name, text, repo / "guide.md", repo)
    assert refs == []
