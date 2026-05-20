"""Tests for xray/languages.py: language registry and extension mapping."""

from unittest.mock import patch


class TestSupportedLanguages:
    """Tests for SUPPORTED_LANGUAGES constant."""

    def test_mandatory_languages_present(self) -> None:
        """All 10 mandatory languages must be in SUPPORTED_LANGUAGES."""
        from code_indexer.xray.languages import SUPPORTED_LANGUAGES

        mandatory = {
            "java",
            "kotlin",
            "go",
            "python",
            "typescript",
            "javascript",
            "bash",
            "csharp",
            "html",
            "css",
        }
        for lang in mandatory:
            assert lang in SUPPORTED_LANGUAGES, f"Mandatory language '{lang}' missing"

    def test_supported_languages_is_dict(self) -> None:
        """SUPPORTED_LANGUAGES must be a dict mapping canonical name to grammar name."""
        from code_indexer.xray.languages import SUPPORTED_LANGUAGES

        assert isinstance(SUPPORTED_LANGUAGES, dict)

    def test_csharp_maps_to_c_sharp_grammar(self) -> None:
        """csharp canonical name must map to c_sharp (the grammar package name)."""
        from code_indexer.xray.languages import SUPPORTED_LANGUAGES

        assert SUPPORTED_LANGUAGES["csharp"] == "c_sharp"

    def test_python_grammar_name(self) -> None:
        """python canonical name maps to python grammar."""
        from code_indexer.xray.languages import SUPPORTED_LANGUAGES

        assert SUPPORTED_LANGUAGES["python"] == "python"

    def test_javascript_grammar_name(self) -> None:
        from code_indexer.xray.languages import SUPPORTED_LANGUAGES

        assert SUPPORTED_LANGUAGES["javascript"] == "javascript"

    def test_typescript_grammar_name(self) -> None:
        from code_indexer.xray.languages import SUPPORTED_LANGUAGES

        assert SUPPORTED_LANGUAGES["typescript"] == "typescript"


class TestExtensionMap:
    """Tests for EXTENSION_MAP constant."""

    def test_extension_map_is_dict(self) -> None:
        from code_indexer.xray.languages import EXTENSION_MAP

        assert isinstance(EXTENSION_MAP, dict)

    def test_java_extension(self) -> None:
        from code_indexer.xray.languages import EXTENSION_MAP

        assert EXTENSION_MAP[".java"] == "java"

    def test_kotlin_extension(self) -> None:
        from code_indexer.xray.languages import EXTENSION_MAP

        assert EXTENSION_MAP[".kt"] == "kotlin"

    def test_go_extension(self) -> None:
        from code_indexer.xray.languages import EXTENSION_MAP

        assert EXTENSION_MAP[".go"] == "go"

    def test_python_extension(self) -> None:
        from code_indexer.xray.languages import EXTENSION_MAP

        assert EXTENSION_MAP[".py"] == "python"

    def test_typescript_extensions(self) -> None:
        from code_indexer.xray.languages import EXTENSION_MAP

        assert EXTENSION_MAP[".ts"] == "typescript"
        assert EXTENSION_MAP[".tsx"] == "typescript"

    def test_javascript_extensions(self) -> None:
        from code_indexer.xray.languages import EXTENSION_MAP

        assert EXTENSION_MAP[".js"] == "javascript"
        assert EXTENSION_MAP[".mjs"] == "javascript"
        assert EXTENSION_MAP[".cjs"] == "javascript"

    def test_bash_extensions(self) -> None:
        from code_indexer.xray.languages import EXTENSION_MAP

        assert EXTENSION_MAP[".sh"] == "bash"
        assert EXTENSION_MAP[".bash"] == "bash"

    def test_csharp_extension(self) -> None:
        from code_indexer.xray.languages import EXTENSION_MAP

        assert EXTENSION_MAP[".cs"] == "csharp"

    def test_html_extensions(self) -> None:
        from code_indexer.xray.languages import EXTENSION_MAP

        assert EXTENSION_MAP[".html"] == "html"
        assert EXTENSION_MAP[".htm"] == "html"

    def test_css_extension(self) -> None:
        from code_indexer.xray.languages import EXTENSION_MAP

        assert EXTENSION_MAP[".css"] == "css"

    def test_all_extensions_lowercase(self) -> None:
        """All extension keys must be lowercase (matching Path.suffix.lower())."""
        from code_indexer.xray.languages import EXTENSION_MAP

        for ext in EXTENSION_MAP:
            assert ext == ext.lower(), f"Extension '{ext}' is not lowercase"
            assert ext.startswith("."), f"Extension '{ext}' must start with '.'"


class TestHclAvailable:
    """Tests for _hcl_available() conditional HCL detection."""

    def test_hcl_unavailable_when_get_language_raises(self) -> None:
        """_hcl_available() returns False when tree_sitter_languages.get_language('hcl') raises."""
        from code_indexer.xray.languages import _hcl_available

        with patch(
            "tree_sitter_languages.get_language",
            side_effect=Exception("hcl grammar not available"),
        ):
            result = _hcl_available()
        assert result is False

    def test_hcl_available_function_exists(self) -> None:
        """_hcl_available must be importable from languages module."""
        from code_indexer.xray.languages import _hcl_available

        assert callable(_hcl_available)

    def test_hcl_available_returns_bool(self) -> None:
        """_hcl_available() always returns a bool."""
        from code_indexer.xray.languages import _hcl_available

        result = _hcl_available()
        assert isinstance(result, bool)

    def test_terraform_extension_when_hcl_available(self) -> None:
        """When HCL is available, .tf extension maps to 'terraform' at instance level."""
        from code_indexer.xray.languages import _hcl_available

        if _hcl_available():
            from code_indexer.xray.ast_engine import AstSearchEngine

            engine = AstSearchEngine()
            assert engine.extension_map.get(".tf") == "terraform"

    def test_terraform_extension_absent_when_hcl_unavailable(self) -> None:
        """When HCL unavailable (our test environment), .tf not in EXTENSION_MAP."""
        from code_indexer.xray.languages import _hcl_available, EXTENSION_MAP

        if not _hcl_available():
            assert ".tf" not in EXTENSION_MAP


class TestTerraformConditionalEngine:
    """Deterministic tests for terraform conditional wiring in AstSearchEngine.

    These tests patch tree_sitter_languages.get_language to control whether
    HCL is reported as available, verifying that AstSearchEngine.supported_languages
    and extension_map respond dynamically to HCL availability.
    """

    def test_supported_languages_excludes_terraform_when_hcl_absent(self) -> None:
        """When get_language('hcl') raises, terraform must be absent from engine."""
        from code_indexer.xray.ast_engine import AstSearchEngine

        with patch(
            "tree_sitter_languages.get_language",
            side_effect=Exception("hcl grammar not available"),
        ):
            engine = AstSearchEngine()
        assert "terraform" not in engine.supported_languages
        assert engine.extension_map.get(".tf") is None

    def test_supported_languages_includes_terraform_when_hcl_present(self) -> None:
        """When get_language('hcl') succeeds, terraform MUST appear in engine."""
        from code_indexer.xray.ast_engine import AstSearchEngine

        # The real tree_sitter_languages has HCL bundled (verified on this system),
        # so instantiating without any patch is the definitive "available" test.
        engine = AstSearchEngine()
        assert "terraform" in engine.supported_languages
        assert engine.extension_map.get(".tf") == "terraform"
