"""E2E tests verifying multimodal HNSW index files are created - Story #66 Regression Prevention."""

import shutil
import subprocess
import pytest


@pytest.mark.e2e
class TestMultimodalIndexFilesCreated:
    """Test that multimodal indexing creates BOTH voyage-code-3 and voyage-multimodal-3 collections.

    This test prevents regression of the bug where multimodal HNSW was never built due to
    incorrect collection name handling in the HNSW builder.
    """

    def test_both_hnsw_indexes_created_after_indexing(self, multimodal_repo_path):
        """Verify BOTH voyage-code-3 and voyage-multimodal-3 HNSW indexes exist after indexing.

        This is the critical regression test for the multimodal HNSW fix.
        """
        # Clean any existing index
        code_indexer_dir = multimodal_repo_path / ".code-indexer"
        if code_indexer_dir.exists():
            shutil.rmtree(code_indexer_dir)

        # Run cidx init
        result = subprocess.run(
            ["cidx", "init"],
            cwd=multimodal_repo_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"cidx init failed: {result.stderr}"

        # Run cidx index (multimodal is automatic when images are present)
        result = subprocess.run(
            ["cidx", "index"],
            cwd=multimodal_repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"cidx index failed: {result.stderr}"

        # Verify BOTH index directories exist
        index_dir = multimodal_repo_path / ".code-indexer" / "index"
        assert index_dir.exists(), f"Index directory missing: {index_dir}"

        voyage_code_dir = index_dir / "voyage-code-3"
        voyage_multimodal_dir = index_dir / "voyage-multimodal-3"

        assert voyage_code_dir.exists(), (
            f"voyage-code-3 collection directory missing: {voyage_code_dir}"
        )
        assert voyage_multimodal_dir.exists(), (
            f"voyage-multimodal-3 collection directory missing: {voyage_multimodal_dir}"
        )

        # Verify HNSW index files exist for BOTH collections
        code_hnsw = voyage_code_dir / "hnsw_index.bin"
        multimodal_hnsw = voyage_multimodal_dir / "hnsw_index.bin"

        assert code_hnsw.exists(), (
            f"voyage-code-3 HNSW index missing: {code_hnsw}"
        )
        assert multimodal_hnsw.exists(), (
            f"voyage-multimodal-3 HNSW index missing: {multimodal_hnsw}"
        )

        # Verify id_index.bin exists and has content for multimodal collection
        multimodal_id_index = voyage_multimodal_dir / "id_index.bin"
        assert multimodal_id_index.exists(), (
            f"voyage-multimodal-3 id_index.bin missing: {multimodal_id_index}"
        )

        # Verify id_index.bin has actual content (size > 0)
        assert multimodal_id_index.stat().st_size > 0, (
            f"voyage-multimodal-3 id_index.bin is empty - no vectors indexed"
        )

    def test_multimodal_hnsw_has_vectors(self, multimodal_repo_path):
        """Verify multimodal HNSW index contains vectors (non-empty index).

        This ensures the HNSW builder actually processed multimodal vectors.
        """
        # Ensure indexing has been done (from previous test or setup)
        multimodal_hnsw = (
            multimodal_repo_path / ".code-indexer" / "index" /
            "voyage-multimodal-3" / "hnsw_index.bin"
        )

        if not multimodal_hnsw.exists():
            # Run indexing if needed
            subprocess.run(
                ["cidx", "init"],
                cwd=multimodal_repo_path,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["cidx", "index"],
                cwd=multimodal_repo_path,
                capture_output=True,
                check=True,
                timeout=120,
            )

        # Verify HNSW index file exists and is non-empty
        assert multimodal_hnsw.exists(), (
            f"voyage-multimodal-3 HNSW index missing: {multimodal_hnsw}"
        )
        assert multimodal_hnsw.stat().st_size > 0, (
            "voyage-multimodal-3 HNSW index is empty - no vectors built"
        )

    def test_multimodal_collection_has_document_ids(self, multimodal_repo_path):
        """Verify multimodal collection has document IDs mapped to vectors.

        This tests that the id_index.bin contains actual document-to-vector mappings.
        """
        # Ensure indexing has been done
        multimodal_id_index = (
            multimodal_repo_path / ".code-indexer" / "index" /
            "voyage-multimodal-3" / "id_index.bin"
        )

        if not multimodal_id_index.exists():
            subprocess.run(
                ["cidx", "init"],
                cwd=multimodal_repo_path,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["cidx", "index"],
                cwd=multimodal_repo_path,
                capture_output=True,
                check=True,
                timeout=120,
            )

        # Verify id_index.bin exists and has content
        assert multimodal_id_index.exists(), (
            f"voyage-multimodal-3 id_index.bin missing: {multimodal_id_index}"
        )

        file_size = multimodal_id_index.stat().st_size
        assert file_size > 100, (  # Arbitrary threshold, real index should be much larger
            f"voyage-multimodal-3 id_index.bin suspiciously small ({file_size} bytes) - "
            "likely no documents mapped to vectors"
        )
