"""
Tests for DependencyMapAnalyzer JSON extraction from Claude CLI output.

Tests the _extract_json method's ability to handle:
- Pure JSON (no preamble)
- JSON with markdown code fences
- JSON with natural language preamble
- JSON with both preamble and code fences
- Error cases (no JSON, empty string)
- Nested JSON objects
- JSON with trailing text
"""

import pytest

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer


class TestJsonExtraction:
    """Test suite for _extract_json static method."""

    @pytest.fixture
    def analyzer(self, tmp_path):
        """Create DependencyMapAnalyzer instance for testing."""
        golden_repos_root = tmp_path / "golden-repos"
        cidx_meta_path = tmp_path / "cidx-meta"
        golden_repos_root.mkdir()
        cidx_meta_path.mkdir()
        return DependencyMapAnalyzer(
            golden_repos_root=golden_repos_root,
            cidx_meta_path=cidx_meta_path,
            pass_timeout=60,
        )

    def test_pure_json_array_no_preamble(self, analyzer):
        """Test extraction of pure JSON array without any preamble."""
        output = '[{"name": "domain1", "description": "desc1", "participating_repos": ["repo1"]}]'
        result = analyzer._extract_json(output)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "domain1"
        assert result[0]["description"] == "desc1"
        assert result[0]["participating_repos"] == ["repo1"]

    def test_json_with_markdown_code_fences(self, analyzer):
        """Test extraction of JSON wrapped in markdown code fences."""
        output = '''```json
[
  {"name": "auth", "description": "Authentication domain", "participating_repos": ["auth-service"]}
]
```'''
        result = analyzer._extract_json(output)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "auth"

    def test_json_with_natural_language_preamble(self, analyzer):
        """Test extraction of JSON with natural language preamble before JSON."""
        output = '''Based on my analysis of the repositories, their dependencies, integration patterns, and shared technologies, I can now identify the domain clusters:

[
  {"name": "core-infrastructure", "description": "Core services", "participating_repos": ["repo1", "repo2"]},
  {"name": "data-processing", "description": "Data pipeline", "participating_repos": ["repo3"]}
]'''
        result = analyzer._extract_json(output)

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "core-infrastructure"
        assert result[1]["name"] == "data-processing"

    def test_json_with_preamble_and_code_fences(self, analyzer):
        """Test extraction of JSON with both preamble AND markdown code fences."""
        output = '''After analyzing all repositories, here are the domain clusters:

```json
[
  {"name": "web-stack", "description": "Web application layer", "participating_repos": ["frontend", "backend"]}
]
```'''
        result = analyzer._extract_json(output)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "web-stack"
        assert result[0]["participating_repos"] == ["frontend", "backend"]

    def test_empty_string_raises_value_error(self, analyzer):
        """Test that empty string raises ValueError."""
        with pytest.raises(ValueError, match="No JSON found"):
            analyzer._extract_json("")

    def test_no_json_at_all_raises_value_error(self, analyzer):
        """Test that text without any JSON raises ValueError."""
        output = "This is just plain text without any JSON structure."

        with pytest.raises(ValueError, match="No JSON found"):
            analyzer._extract_json(output)

    def test_nested_json_objects(self, analyzer):
        """Test extraction of complex nested JSON structures."""
        output = '''Here's the analysis:

[
  {
    "name": "api-layer",
    "description": "API services",
    "participating_repos": ["api-gateway", "auth-service"],
    "metadata": {
      "complexity": "high",
      "dependencies": {
        "internal": ["database", "cache"],
        "external": ["aws-s3"]
      }
    }
  }
]'''
        result = analyzer._extract_json(output)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "api-layer"
        assert result[0]["metadata"]["complexity"] == "high"
        assert result[0]["metadata"]["dependencies"]["internal"] == ["database", "cache"]

    def test_json_with_trailing_text(self, analyzer):
        """Test extraction of JSON with trailing text after closing bracket."""
        output = '''[
  {"name": "domain1", "description": "desc1", "participating_repos": ["repo1"]}
]

Some additional text after the JSON that should be ignored.'''

        result = analyzer._extract_json(output)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "domain1"

    def test_json_with_brackets_in_string_values(self, analyzer):
        """Test extraction of JSON with brackets inside string values (Issue 4)."""
        output = 'Here is the analysis:\n\n[{"name": "data-processing", "description": "Handles [ETL] pipelines", "participating_repos": ["repo1"]}]'
        result = analyzer._extract_json(output)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["description"] == "Handles [ETL] pipelines"

    def test_json_with_escaped_quotes(self, analyzer):
        """Test extraction of JSON with escaped quotes in strings (Issue 5)."""
        output = '[{"name": "test", "description": "Uses \\"special\\" syntax"}]'
        result = analyzer._extract_json(output)
        assert result[0]["description"] == 'Uses "special" syntax'

    def test_json_object_extraction(self, analyzer):
        """Test extraction of top-level JSON object (dict) not just arrays (Issue 6)."""
        output = 'Here is the result:\n\n{"domain": "auth", "repos": ["repo1"]}'
        result = analyzer._extract_json(output)
        assert isinstance(result, dict)
        assert result["domain"] == "auth"
