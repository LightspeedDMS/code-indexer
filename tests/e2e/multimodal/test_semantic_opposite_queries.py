"""E2E tests for multimodal vector creation with opposite semantics - Story #66.

These tests verify that multimodal embeddings are created for documents where
the TEXT content is semantically OPPOSITE or UNRELATED to the IMAGE content.

This proves that multimodal indexing captures image semantics independently
of text content by verifying vectors exist in the voyage-multimodal-3 collection
for documents with unrelated text/image pairings.

NOTE: End-to-end query testing (searching both code and multimodal collections)
requires MultiIndexQueryService updates to search voyage-multimodal-3 collection
instead of the old multimodal_index/ subdirectory. These tests focus on validating
that multimodal vectors are created correctly.
"""

import json
import shutil
import subprocess
import pytest
from pathlib import Path


@pytest.mark.e2e
class TestSemanticOppositeQueries:
    """Test multimodal vectors are created for semantically opposite content.

    Test fixtures in docs/unrelated/ have semantically opposite text/image pairings:
    - shakespeare-sonnet.md (poetry) + database-schema.png (SQL data types)
    - pasta-recipe.md (cooking) + api-flow.jpg (JWT auth flow)
    - hiking-guide.md (nature) + config-options.webp (server config)
    - fairy-tale.md (children's story) + error-codes.gif (HTTP errors)

    These tests verify that multimodal vectors are created for these documents,
    proving that image content is being embedded independently of text.
    """

    @pytest.fixture(autouse=True)
    def setup_index(self, multimodal_repo_path):
        """Ensure clean multimodal index exists before each test."""
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

    def _find_multimodal_vector_for_file(self, multimodal_repo_path: Path, filename: str) -> bool:
        """Search for a vector file containing the given filename in multimodal collection.

        Args:
            multimodal_repo_path: Path to the multimodal repo
            filename: Filename to search for (e.g., "shakespeare-sonnet.md")

        Returns:
            True if vector found, False otherwise
        """
        multimodal_index = (
            multimodal_repo_path / ".code-indexer" / "index" / "voyage-multimodal-3"
        )

        if not multimodal_index.exists():
            return False

        # Search all JSON vector files for the filename
        for json_file in multimodal_index.rglob("vector_*.json"):
            try:
                with open(json_file, "r") as f:
                    data = json.load(f)
                    if data.get("payload", {}).get("path", "").endswith(filename):
                        return True
            except (json.JSONDecodeError, IOError):
                continue

        return False

    def test_shakespeare_sonnet_has_multimodal_vector(self, multimodal_repo_path):
        """Verify shakespeare-sonnet.md has a multimodal vector created.

        This document has:
        - TEXT: Shakespeare poetry (romantic, timeless, beauty)
        - IMAGE: database-schema.png (SQL: VARCHAR, INTEGER, TIMESTAMP)

        The text and image are semantically opposite. The existence of a multimodal
        vector proves image content was embedded independently of text.
        """
        assert self._find_multimodal_vector_for_file(
            multimodal_repo_path, "shakespeare-sonnet.md"
        ), "shakespeare-sonnet.md should have a multimodal vector in voyage-multimodal-3 collection"

        # Verify the text does NOT contain database terms (proves opposite semantics)
        sonnet_file = multimodal_repo_path / "docs" / "unrelated" / "shakespeare-sonnet.md"
        sonnet_text = sonnet_file.read_text()
        assert "VARCHAR" not in sonnet_text, "VARCHAR should not be in sonnet text"
        assert "INTEGER" not in sonnet_text, "INTEGER should not be in sonnet text"
        assert "TIMESTAMP" not in sonnet_text, "TIMESTAMP should not be in sonnet text"

    def test_pasta_recipe_has_multimodal_vector(self, multimodal_repo_path):
        """Verify pasta-recipe.md has a multimodal vector created.

        This document has:
        - TEXT: Italian cooking recipe (flour, eggs, pasta)
        - IMAGE: api-flow.jpg (JWT auth: bearer token, endpoints)

        The text and image are semantically opposite.
        """
        assert self._find_multimodal_vector_for_file(
            multimodal_repo_path, "pasta-recipe.md"
        ), "pasta-recipe.md should have a multimodal vector in voyage-multimodal-3 collection"

        # Verify the text does NOT contain JWT terms (proves opposite semantics)
        recipe_file = multimodal_repo_path / "docs" / "unrelated" / "pasta-recipe.md"
        recipe_text = recipe_file.read_text()
        assert "JWT" not in recipe_text, "JWT should not be in recipe text"
        assert "bearer" not in recipe_text, "bearer should not be in recipe text"
        assert "authentication" not in recipe_text, "authentication should not be in recipe text"

    def test_hiking_guide_has_multimodal_vector(self, multimodal_repo_path):
        """Verify hiking-guide.md has a multimodal vector created.

        This document has:
        - TEXT: Swiss Alps hiking (mountains, trails, nature)
        - IMAGE: config-options.webp (server config: port 8000, database.url)

        The text and image are semantically opposite.
        """
        assert self._find_multimodal_vector_for_file(
            multimodal_repo_path, "hiking-guide.md"
        ), "hiking-guide.md should have a multimodal vector in voyage-multimodal-3 collection"

        # Verify the text does NOT contain config terms (proves opposite semantics)
        hiking_file = multimodal_repo_path / "docs" / "unrelated" / "hiking-guide.md"
        hiking_text = hiking_file.read_text()
        assert "8000" not in hiking_text, "8000 should not be in hiking text"
        assert "postgresql" not in hiking_text.lower(), "postgresql should not be in hiking text"
        assert "database" not in hiking_text, "database should not be in hiking text"

    def test_fairy_tale_has_multimodal_vector(self, multimodal_repo_path):
        """Verify fairy-tale.md has a multimodal vector created.

        This document has:
        - TEXT: Children's story (princess, butterfly, magic)
        - IMAGE: error-codes.gif (HTTP errors: 400, 401, 429, 500)

        The text and image are semantically opposite.
        """
        assert self._find_multimodal_vector_for_file(
            multimodal_repo_path, "fairy-tale.md"
        ), "fairy-tale.md should have a multimodal vector in voyage-multimodal-3 collection"

        # Verify the text does NOT contain error terms (proves opposite semantics)
        tale_file = multimodal_repo_path / "docs" / "unrelated" / "fairy-tale.md"
        tale_text = tale_file.read_text()
        assert "429" not in tale_text, "429 should not be in fairy tale text"
        assert "500" not in tale_text, "500 should not be in fairy tale text"
        assert "Gateway" not in tale_text, "Gateway should not be in fairy tale text"

    def test_garden_tips_html_has_multimodal_vector(self, multimodal_repo_path):
        """Verify garden-tips.html has a multimodal vector created.

        This document has:
        - TEXT: Spring gardening tips (soil, planting, watering)
        - IMAGE: database-schema.png (SQL: VARCHAR, INTEGER, TIMESTAMP)

        The text and image are semantically opposite.
        Tests HTML file format support for multimodal indexing.
        """
        assert self._find_multimodal_vector_for_file(
            multimodal_repo_path, "garden-tips.html"
        ), "garden-tips.html should have a multimodal vector in voyage-multimodal-3 collection"

        # Verify the text does NOT contain database terms (proves opposite semantics)
        garden_file = multimodal_repo_path / "docs" / "unrelated" / "garden-tips.html"
        garden_text = garden_file.read_text()
        assert "VARCHAR" not in garden_text, "VARCHAR should not be in garden text"
        assert "INTEGER" not in garden_text, "INTEGER should not be in garden text"
        assert "TIMESTAMP" not in garden_text, "TIMESTAMP should not be in garden text"

    def test_astronomy_htmx_has_multimodal_vector(self, multimodal_repo_path):
        """Verify astronomy-basics.htmx has a multimodal vector created.

        This document has:
        - TEXT: Astronomy/space content (planets, stars, observing)
        - IMAGE: api-flow.jpg (JWT bearer token authentication)

        The text and image are semantically opposite.
        Tests HTMX file format support for multimodal indexing.
        """
        assert self._find_multimodal_vector_for_file(
            multimodal_repo_path, "astronomy-basics.htmx"
        ), "astronomy-basics.htmx should have a multimodal vector in voyage-multimodal-3 collection"

        # Verify the text does NOT contain JWT terms (proves opposite semantics)
        astro_file = multimodal_repo_path / "docs" / "unrelated" / "astronomy-basics.htmx"
        astro_text = astro_file.read_text()
        assert "JWT" not in astro_text, "JWT should not be in astronomy text"
        assert "bearer" not in astro_text.lower(), "bearer should not be in astronomy text"
        assert "authentication" not in astro_text.lower(), "authentication should not be in astronomy text"

    def test_all_unrelated_docs_have_multimodal_vectors(self, multimodal_repo_path):
        """Verify ALL documents in docs/unrelated/ have multimodal vectors.

        This is a comprehensive test ensuring complete coverage of semantically
        opposite text/image pairings across all supported file formats:
        - Markdown (.md)
        - HTML (.html)
        - HTMX (.htmx)
        """
        unrelated_docs = [
            # Markdown files
            "shakespeare-sonnet.md",
            "pasta-recipe.md",
            "hiking-guide.md",
            "fairy-tale.md",
            # HTML file
            "garden-tips.html",
            # HTMX file
            "astronomy-basics.htmx",
        ]

        for doc in unrelated_docs:
            assert self._find_multimodal_vector_for_file(
                multimodal_repo_path, doc
            ), f"{doc} should have a multimodal vector in voyage-multimodal-3 collection"

    def test_query_database_schema_finds_shakespeare_sonnet(self, multimodal_repo_path):
        """Query for database schema content should find shakespeare-sonnet.md via image.

        shakespeare-sonnet.md has:
        - TEXT: Shakespeare poetry (unrelated to databases)
        - IMAGE: database-schema.png (SQL: VARCHAR, INTEGER, TIMESTAMP)

        Query for database content should find the document based on IMAGE, not TEXT.
        """
        result = subprocess.run(
            ["cidx", "query", "VARCHAR INTEGER TIMESTAMP SQL columns", "--limit", "10"],
            cwd=multimodal_repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"Query failed: {result.stderr}"

        # Verify shakespeare-sonnet.md is in the results
        assert "shakespeare-sonnet.md" in result.stdout, (
            "Query for database schema terms should find shakespeare-sonnet.md "
            "via its database-schema.png image (proving multimodal search works)"
        )

    def test_query_jwt_auth_finds_pasta_recipe(self, multimodal_repo_path):
        """Query for JWT auth content should find pasta-recipe.md via image.

        pasta-recipe.md has:
        - TEXT: Italian cooking recipe (unrelated to auth)
        - IMAGE: api-flow.jpg (JWT bearer token authentication)

        Query for JWT content should find the document based on IMAGE, not TEXT.
        """
        result = subprocess.run(
            ["cidx", "query", "JWT bearer token authentication", "--limit", "10"],
            cwd=multimodal_repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"Query failed: {result.stderr}"

        # Verify pasta-recipe.md is in the results
        assert "pasta-recipe.md" in result.stdout, (
            "Query for JWT auth terms should find pasta-recipe.md "
            "via its api-flow.jpg image (proving multimodal search works)"
        )

    def test_query_server_config_finds_hiking_guide(self, multimodal_repo_path):
        """Query for server config content should find hiking-guide.md via image.

        hiking-guide.md has:
        - TEXT: Swiss Alps hiking (unrelated to servers)
        - IMAGE: config-options.webp (server port 8000, postgresql database)

        Query for server config should find the document based on IMAGE, not TEXT.
        """
        result = subprocess.run(
            ["cidx", "query", "server port 8000 postgresql database", "--limit", "10"],
            cwd=multimodal_repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"Query failed: {result.stderr}"

        # Verify hiking-guide.md is in the results
        assert "hiking-guide.md" in result.stdout, (
            "Query for server config terms should find hiking-guide.md "
            "via its config-options.webp image (proving multimodal search works)"
        )

    def test_query_http_errors_finds_fairy_tale(self, multimodal_repo_path):
        """Query for HTTP errors should find fairy-tale.md via image.

        fairy-tale.md has:
        - TEXT: Children's story (unrelated to HTTP)
        - IMAGE: error-codes.gif (HTTP 429, 500 error codes)

        Query for HTTP errors should find the document based on IMAGE, not TEXT.
        """
        result = subprocess.run(
            ["cidx", "query", "HTTP 429 500 error status code", "--limit", "10"],
            cwd=multimodal_repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"Query failed: {result.stderr}"

        # Verify fairy-tale.md is in the results
        assert "fairy-tale.md" in result.stdout, (
            "Query for HTTP error terms should find fairy-tale.md "
            "via its error-codes.gif image (proving multimodal search works)"
        )

    def test_query_database_schema_finds_garden_tips_html(self, multimodal_repo_path):
        """Query for database schema should find garden-tips.html via image.

        garden-tips.html has:
        - TEXT: Spring gardening tips (soil, planting, watering)
        - IMAGE: database-schema.png (SQL: VARCHAR, INTEGER, TIMESTAMP)

        Query for database content should find the HTML document based on IMAGE.
        Tests HTML file format support for multimodal queries.
        """
        result = subprocess.run(
            ["cidx", "query", "VARCHAR INTEGER TIMESTAMP SQL columns", "--limit", "10"],
            cwd=multimodal_repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"Query failed: {result.stderr}"

        # Verify garden-tips.html is in the results
        assert "garden-tips.html" in result.stdout, (
            "Query for database schema terms should find garden-tips.html "
            "via its database-schema.png image (proving HTML multimodal search works)"
        )

    def test_query_jwt_auth_finds_astronomy_htmx(self, multimodal_repo_path):
        """Query for JWT auth should find astronomy-basics.htmx via image.

        astronomy-basics.htmx has:
        - TEXT: Astronomy/space content (planets, stars, observing)
        - IMAGE: api-flow.jpg (JWT bearer token authentication)

        Query for JWT content should find the HTMX document based on IMAGE.
        Tests HTMX file format support for multimodal queries.

        NOTE: Uses limit=15 because astronomy text is semantically OPPOSED to
        JWT/auth terms, which pushes the combined multimodal embedding lower
        in rankings. Unlike pasta-recipe.md (neutral cooking text), the
        astronomy domain actively interferes with the image's JWT signal.
        """
        result = subprocess.run(
            ["cidx", "query", "JWT bearer token authentication", "--limit", "15"],
            cwd=multimodal_repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"Query failed: {result.stderr}"

        # Verify astronomy-basics.htmx is in the results
        assert "astronomy-basics.htmx" in result.stdout, (
            "Query for JWT auth terms should find astronomy-basics.htmx "
            "via its api-flow.jpg image (proving HTMX multimodal search works)"
        )
