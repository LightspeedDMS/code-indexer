"""Test to verify chunk image structure - Evidence for bug fix.

This test proves that chunk['images'] is a list of strings, not a list of dicts.
Related to file_chunking_manager.py line 566 bug.
"""

from pathlib import Path

from code_indexer.indexing.chunker import TextChunker
from code_indexer.config import IndexingConfig


# Compute paths relative to test file location
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
TEST_FILE = PROJECT_ROOT / "test-fixtures/multimodal-mock-repo/docs/database-guide.md"
REPO_ROOT = PROJECT_ROOT / "test-fixtures/multimodal-mock-repo"


def test_chunk_images_field_is_list_of_strings():
    """Verify that chunk['images'] contains strings, not dicts."""
    # Arrange
    config = IndexingConfig()
    chunker = TextChunker(config)

    assert TEST_FILE.exists(), f"Test file not found: {TEST_FILE}"
    assert REPO_ROOT.exists(), f"Repo root not found: {REPO_ROOT}"

    # Act
    chunks = chunker.chunk_file(TEST_FILE, repo_root=REPO_ROOT)

    # Assert
    assert len(chunks) > 0, "Expected at least one chunk"

    # Check first chunk structure
    chunk = chunks[0]
    assert "images" in chunk, "Expected 'images' field in chunk"

    images = chunk["images"]
    assert isinstance(images, list), f"Expected images to be a list, got {type(images)}"

    # This is the critical assertion: images should be a list of strings
    if len(images) > 0:
        for img_ref in images:
            assert isinstance(
                img_ref, str
            ), f"Expected img_ref to be a string, got {type(img_ref)}: {img_ref}"
            # Should be a relative path string like "images/database-schema.png"
            assert not isinstance(img_ref, dict), "img_ref should NOT be a dict"

        # Verify the actual content
        assert (
            "images/database-schema.png" in images
        ), f"Expected image path in {images}"

    print(f"\nEVIDENCE: chunk['images'] = {images}")
    print(f"Type: {type(images)}")
    if images:
        print(f"First element type: {type(images[0])}")
        print(f"First element value: {images[0]}")


def test_chunk_images_usage_in_file_chunking_manager():
    """Demonstrate correct usage pattern for file_chunking_manager.py line 566."""
    config = IndexingConfig()
    chunker = TextChunker(config)

    chunks = chunker.chunk_file(TEST_FILE, repo_root=REPO_ROOT)
    chunk = chunks[0]
    codebase_dir = REPO_ROOT

    # CORRECT USAGE (what should be in file_chunking_manager.py):
    for img_ref in chunk.get("images", []):
        # img_ref is a STRING, not a dict
        img_path = codebase_dir / img_ref  # CORRECT - use img_ref directly
        assert isinstance(img_path, Path)
        print(f"CORRECT: img_path = codebase_dir / '{img_ref}' = {img_path}")

    # WRONG USAGE (current bug at line 566):
    # for img_ref in chunk.get('images', []):
    #     img_path = codebase_dir / img_ref["path"]  # WRONG - img_ref is not a dict!
