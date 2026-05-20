"""Tests for Bug #1014: _hcl_available() probes tree_sitter_languages, not tree_sitter_hcl."""

from __future__ import annotations

from unittest.mock import patch


class TestHclAvailableProbesCorrectPackage:
    def test_hcl_available_returns_true_with_tree_sitter_languages(self) -> None:
        from code_indexer.xray.languages import _hcl_available

        assert _hcl_available() is True

    def test_hcl_available_returns_false_when_get_language_raises(self) -> None:
        from code_indexer.xray.languages import _hcl_available

        with patch(
            "tree_sitter_languages.get_language",
            side_effect=Exception("hcl grammar not available"),
        ):
            result = _hcl_available()
        assert result is False


class TestTerraformInAstEngineInstance:
    def test_tf_maps_to_terraform_in_engine_extension_map(self) -> None:
        from code_indexer.xray.ast_engine import AstSearchEngine

        engine = AstSearchEngine()
        assert engine.extension_map.get(".tf") == "terraform"

    def test_terraform_in_engine_supported_languages(self) -> None:
        from code_indexer.xray.ast_engine import AstSearchEngine

        engine = AstSearchEngine()
        assert "terraform" in engine.supported_languages

    def test_terraform_supported_languages_maps_to_hcl_grammar(self) -> None:
        from code_indexer.xray.ast_engine import AstSearchEngine

        engine = AstSearchEngine()
        assert engine.supported_languages.get("terraform") == "hcl"


class TestParseTerraformSnippet:
    def test_parse_tf_snippet_through_ast_engine(self) -> None:
        from code_indexer.xray.ast_engine import AstSearchEngine

        engine = AstSearchEngine()
        tf_source = 'resource "aws_instance" "example" {\n  ami = "ami-0abcdef"\n}\n'
        root = engine.parse(tf_source, "terraform")
        assert root is not None
        assert root.type is not None
        assert root.start_byte == 0
