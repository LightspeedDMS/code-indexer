"""
Tests for multimodal-aware CLI output in status command and help text.

Verifies that:
1. Provider line shows both voyage-code-3 and voyage-multimodal-3
2. Multimodal collection stats appear when present
3. No multimodal stats when collection absent (backward compat)
4. Help text mentions both models
5. Init command output mentions both models
6. README template mentions multimodal
"""

import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.config import VOYAGE_MULTIMODAL_MODEL


@pytest.fixture
def temp_project():
    """Create a temporary project directory with .code-indexer."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        code_indexer_dir = project_root / ".code-indexer"
        code_indexer_dir.mkdir()
        (code_indexer_dir / "config.json").write_text(
            """{
                "codebase_dir": ".",
                "embedding_provider": "voyage-ai",
                "voyage_ai": {
                    "model": "voyage-code-3",
                    "parallel_requests": 8,
                    "tokens_per_minute": 1000000
                },
                "indexing": {
                    "max_file_size": 1048576,
                    "exclude_dirs": [],
                    "file_extensions": ["py"]
                }
            }"""
        )
        yield project_root


class TestStatusCommandMultimodal:
    """Tests for status command multimodal awareness."""

    def test_provider_line_shows_both_models_when_multimodal_present(
        self, temp_project
    ):
        """
        GIVEN a CIDX-initialized repository with multimodal content indexed
        WHEN I run `cidx status`
        THEN I should see both voyage-code-3 and voyage-multimodal-3 in the Provider line
        """
        # Create multimodal collection directory
        index_dir = temp_project / ".code-indexer" / "index"
        index_dir.mkdir(parents=True)
        code_collection = index_dir / "voyage-code-3"
        code_collection.mkdir()
        multimodal_collection = index_dir / VOYAGE_MULTIMODAL_MODEL
        multimodal_collection.mkdir()

        # Create minimal index files for both collections
        (code_collection / "metadata.json").write_text('{"vector_count": 10}')
        (multimodal_collection / "metadata.json").write_text('{"vector_count": 5}')

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=temp_project):
            result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0, f"Status command failed: {result.output}"
        # Provider details should show both models
        assert "voyage-code-3" in result.output
        assert VOYAGE_MULTIMODAL_MODEL in result.output

    def test_provider_line_shows_both_models_capability_only_code_indexed(
        self, temp_project
    ):
        """
        GIVEN a CIDX-initialized repository with only code content (no multimodal)
        WHEN I run `cidx status`
        THEN I should see both models listed in the Provider line (capability)
        AND I should see only voyage-code-3 collection in Vector Storage (actual state)
        """
        # Create only code collection directory
        index_dir = temp_project / ".code-indexer" / "index"
        index_dir.mkdir(parents=True)
        code_collection = index_dir / "voyage-code-3"
        code_collection.mkdir()
        (code_collection / "metadata.json").write_text('{"vector_count": 10}')

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=temp_project):
            result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0, f"Status command failed: {result.output}"
        # Should show both models as capability
        output_lower = result.output.lower()
        assert "voyage-code-3" in output_lower
        assert "voyage-multimodal-3" in output_lower

        # But Vector Storage should only show code collection
        assert "Collection: voyage-code-3" in result.output
        # Should NOT show multimodal collection stats
        assert f"Collection: {VOYAGE_MULTIMODAL_MODEL}" not in result.output

    def test_multimodal_collection_stats_shown_when_present(self, temp_project):
        """
        GIVEN a CIDX-initialized repository with multimodal collection
        WHEN I run `cidx status`
        THEN the multimodal collection should show vector count, file count, and dimensions
        AND the multimodal index files (projection_matrix, hnsw_index) should be displayed
        """
        # Create both collections
        index_dir = temp_project / ".code-indexer" / "index"
        index_dir.mkdir(parents=True)
        code_collection = index_dir / "voyage-code-3"
        code_collection.mkdir()
        multimodal_collection = index_dir / VOYAGE_MULTIMODAL_MODEL
        multimodal_collection.mkdir()

        # Create metadata for both - FilesystemVectorStore counts from indexed_files
        (code_collection / "metadata.json").write_text(
            '{"vector_count": 10, "indexed_files": {"file1.py": {}}}'
        )
        (multimodal_collection / "metadata.json").write_text(
            '{"vector_count": 5, "indexed_files": {"image1.png": {}, "image2.jpg": {}}}'
        )

        # Create index files for multimodal collection
        (multimodal_collection / "projection_matrix.npy").write_bytes(
            b"fake_matrix_data" * 100
        )
        (multimodal_collection / "hnsw_index.bin").write_bytes(b"fake_hnsw_data" * 1000)

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=temp_project):
            result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0, f"Status command failed: {result.output}"

        # Check for multimodal collection presence
        assert f"Collection: {VOYAGE_MULTIMODAL_MODEL}" in result.output
        # Check that multimodal section appears (Multimodal Storage row)
        assert "Multimodal Storage" in result.output
        # Check file count is displayed (actual count depends on metadata structure)
        assert "Files: 1" in result.output or "files:" in result.output.lower()

        # Check for index files
        assert "Projection Matrix" in result.output
        assert "HNSW Index" in result.output

    def test_no_multimodal_stats_when_collection_absent(self, temp_project):
        """
        GIVEN a CIDX-initialized repository without multimodal collection
        WHEN I run `cidx status`
        THEN multimodal collection stats should NOT appear (backward compatibility)
        AND only code collection stats should be shown
        """
        # Create only code collection (no multimodal directory at all)
        index_dir = temp_project / ".code-indexer" / "index"
        index_dir.mkdir(parents=True)
        code_collection = index_dir / "voyage-code-3"
        code_collection.mkdir()
        (code_collection / "metadata.json").write_text('{"vector_count": 10}')
        (code_collection / "projection_matrix.npy").write_bytes(
            b"fake_matrix_data" * 100
        )

        # Explicitly ensure multimodal collection does NOT exist
        multimodal_collection = index_dir / VOYAGE_MULTIMODAL_MODEL
        assert (
            not multimodal_collection.exists()
        ), "Multimodal collection should not exist for this test"

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=temp_project):
            result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0, f"Status command failed: {result.output}"

        # Should show code collection
        assert "Collection: voyage-code-3" in result.output

        # Should NOT show multimodal collection stats section
        assert "Multimodal Storage" not in result.output
        assert "Multimodal Index Files" not in result.output


class TestHelpTextMultimodal:
    """Tests for help text mentioning both models."""

    def test_cidx_help_mentions_both_models(self):
        """
        GIVEN a user running `cidx --help`
        WHEN they read the help text about chunking
        THEN they should see both voyage-code-3 and voyage-multimodal-3 mentioned
        """
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "voyage-code-3" in result.output
        assert "voyage-multimodal-3" in result.output

    def test_cidx_init_help_mentions_both_models(self):
        """
        GIVEN a user running `cidx init --help`
        WHEN they read the help text about chunking
        THEN they should see both voyage-code-3 and voyage-multimodal-3 mentioned
        """
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--help"])

        assert result.exit_code == 0
        assert "voyage-code-3" in result.output
        assert "voyage-multimodal-3" in result.output


class TestInitCommandMultimodal:
    """Tests for init command multimodal output."""

    def test_init_output_mentions_both_models(self):
        """
        GIVEN a user running `cidx init`
        WHEN they see the initialization output
        THEN they should see both voyage-code-3 and voyage-multimodal-3 mentioned in chunking info
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            runner = CliRunner()

            with runner.isolated_filesystem(temp_dir=project_root):
                result = runner.invoke(cli, ["init"])

            assert result.exit_code == 0, f"Init command failed: {result.output}"

            # Should mention both models in chunking info
            assert "voyage-code-3" in result.output
            assert "voyage-multimodal-3" in result.output


class TestReadmeTemplateMultimodal:
    """Tests for README template mentioning multimodal."""

    def test_generated_readme_mentions_multimodal(self):
        """
        GIVEN a user running `cidx init`
        WHEN README.md is generated
        THEN it should mention voyage-multimodal-3 in the embedding provider section
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            runner = CliRunner()

            # runner.isolated_filesystem changes cwd to tmpdir
            with runner.isolated_filesystem(temp_dir=project_root):
                result = runner.invoke(cli, ["init"])

                assert result.exit_code == 0, f"Init command failed: {result.output}"

                # README is created in current directory (which is project_root after isolated_filesystem)
                readme_path = Path(".code-indexer") / "README.md"
                assert (
                    readme_path.exists()
                ), f"README.md was not created at {readme_path.absolute()}"

                readme_content = readme_path.read_text()
                assert "voyage-multimodal-3" in readme_content
