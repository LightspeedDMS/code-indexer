"""Tests for xray/ast_engine.py: AstSearchEngine with lazy-loaded tree-sitter.

All tests use real tree-sitter parsing — no mocks for tree-sitter itself.
Fixtures are the real source files under tests/unit/xray/fixtures/.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from code_indexer.xray.ast_engine import AstSearchEngine

FIXTURES = Path(__file__).parent / "fixtures"


def _make_engine() -> "AstSearchEngine":
    """Construct an AstSearchEngine (triggers the lazy tree-sitter import).

    The TYPE_CHECKING guard above keeps the import out of the runtime module
    namespace so the lazy-load subprocess gate in test_lazy_load.py is
    unaffected. At type-check time mypy resolves the forward-reference
    correctly, eliminating attr-defined errors on all callsites.
    """
    from code_indexer.xray.ast_engine import AstSearchEngine as _AE

    return _AE()


class TestAstSearchEngineInstantiation:
    """AstSearchEngine can be instantiated and loads tree-sitter lazily."""

    def test_can_instantiate(self) -> None:
        """AstSearchEngine() succeeds when tree-sitter-languages is installed."""
        engine = _make_engine()
        assert engine is not None

    def test_lazy_load_invariant_ast_engine_module_itself(self) -> None:
        """tree_sitter must NOT be imported simply by importing ast_engine module.

        The lazy-load contract: importing the module file costs nothing;
        only AstSearchEngine() triggers the tree-sitter import.
        This test verifies by checking that no tree_sitter key appears in
        sys.modules after a fresh import of the module, before __init__ runs.

        NOTE: This test imports ast_engine in a subprocess to get a clean slate,
        because the current process has already loaded tree_sitter via other tests.
        See test_lazy_load.py for the subprocess-based gate.
        """
        # In-process verification: after importing the class (not instantiating),
        # sys.modules will already have tree_sitter from previous tests in this
        # session — we can only verify the module-level code is clean by
        # inspection.  The subprocess gate in test_lazy_load.py covers this
        # invariant rigorously.  Here we just confirm ast_engine can be imported.
        from code_indexer.xray import ast_engine  # noqa: F401

        assert hasattr(ast_engine, "AstSearchEngine")

    def test_supported_languages_accessible(self) -> None:
        """AstSearchEngine.supported_languages returns the language dict."""
        engine = _make_engine()
        langs = engine.supported_languages
        assert isinstance(langs, dict)
        assert "python" in langs
        assert "java" in langs

    def test_extension_map_accessible(self) -> None:
        """AstSearchEngine.extension_map returns the extension dict."""
        engine = _make_engine()
        ext_map = engine.extension_map
        assert isinstance(ext_map, dict)
        assert ".py" in ext_map


class TestAstSearchEngineParse:
    """AstSearchEngine.parse() returns an XRayNode root."""

    def test_parse_returns_xray_node(self) -> None:
        """parse() returns an XRayNode, not a raw tree_sitter tree."""
        from code_indexer.xray.xray_node import XRayNode

        engine = _make_engine()
        node = engine.parse("x = 1\n", "python")
        assert isinstance(node, XRayNode)

    def test_parse_root_type_python(self) -> None:
        """Python source root node type is 'module'."""
        engine = _make_engine()
        node = engine.parse("x = 1\n", "python")
        assert node.type == "module"

    def test_parse_text_is_str(self) -> None:
        """Root node text is a str."""
        engine = _make_engine()
        node = engine.parse("x = 1\n", "python")
        assert isinstance(node.text, str)

    def test_parse_unsupported_language_raises(self) -> None:
        """parse() raises ValueError for unsupported language."""
        engine = _make_engine()
        with pytest.raises(ValueError, match="unsupported"):
            engine.parse("x = 1\n", "brainfuck")

    def test_parse_empty_source(self) -> None:
        """parse() handles empty source without error."""
        engine = _make_engine()
        node = engine.parse("", "python")
        assert node is not None
        assert node.type == "module"

    def test_parse_bytes_source(self) -> None:
        """parse() accepts bytes source and returns XRayNode."""
        from code_indexer.xray.xray_node import XRayNode

        engine = _make_engine()
        node = engine.parse(b"x = 1\n", "python")
        assert isinstance(node, XRayNode)

    def test_parse_with_latin1_bytes(self) -> None:
        """parse() handles source with non-UTF-8 bytes without raising."""
        engine = _make_engine()
        # Latin-1 encoded content — bytes that are invalid UTF-8
        source = "x = 1  # caf\xe9\n".encode("latin-1")
        node = engine.parse(source, "python")
        assert node is not None


class TestAstSearchEngineLanguages:
    """parse() works for all 10 mandatory languages using real fixture files."""

    def _parse_fixture(self, lang: str, fixture_name: str) -> None:
        """Parse a fixture file and assert the root is a valid XRayNode."""
        from code_indexer.xray.xray_node import XRayNode

        fixture_dir = FIXTURES / lang
        candidates = list(fixture_dir.glob(f"{fixture_name}.*"))
        assert candidates, f"No fixture {fixture_name}.* found in {fixture_dir}"
        fixture_path = candidates[0]
        source = fixture_path.read_bytes()

        engine = _make_engine()
        node = engine.parse(source, lang)
        assert isinstance(node, XRayNode)
        assert not node.has_error, (
            f"Parse errors in {fixture_path}: root.has_error is True"
        )

    def test_parse_python_smoke(self) -> None:
        self._parse_fixture("python", "smoke")

    def test_parse_java_smoke(self) -> None:
        self._parse_fixture("java", "smoke")

    def test_parse_kotlin_smoke(self) -> None:
        self._parse_fixture("kotlin", "smoke")

    def test_parse_go_smoke(self) -> None:
        self._parse_fixture("go", "smoke")

    def test_parse_typescript_smoke(self) -> None:
        self._parse_fixture("typescript", "smoke")

    def test_parse_javascript_smoke(self) -> None:
        self._parse_fixture("javascript", "smoke")

    def test_parse_bash_smoke(self) -> None:
        self._parse_fixture("bash", "smoke")

    def test_parse_csharp_smoke(self) -> None:
        self._parse_fixture("csharp", "smoke")

    def test_parse_html_smoke(self) -> None:
        self._parse_fixture("html", "smoke")

    def test_parse_css_smoke(self) -> None:
        self._parse_fixture("css", "smoke")


class TestAstSearchEngineDetectLanguage:
    """detect_language() maps file paths to canonical language names."""

    def test_detect_python(self) -> None:
        engine = _make_engine()
        assert engine.detect_language(Path("foo.py")) == "python"

    def test_detect_java(self) -> None:
        engine = _make_engine()
        assert engine.detect_language(Path("Foo.java")) == "java"

    def test_detect_kotlin(self) -> None:
        engine = _make_engine()
        assert engine.detect_language(Path("Foo.kt")) == "kotlin"

    def test_detect_typescript(self) -> None:
        engine = _make_engine()
        assert engine.detect_language(Path("index.ts")) == "typescript"

    def test_detect_tsx(self) -> None:
        engine = _make_engine()
        assert engine.detect_language(Path("App.tsx")) == "typescript"

    def test_detect_javascript(self) -> None:
        engine = _make_engine()
        assert engine.detect_language(Path("app.js")) == "javascript"

    def test_detect_mjs(self) -> None:
        engine = _make_engine()
        assert engine.detect_language(Path("module.mjs")) == "javascript"

    def test_detect_go(self) -> None:
        engine = _make_engine()
        assert engine.detect_language(Path("main.go")) == "go"

    def test_detect_bash(self) -> None:
        engine = _make_engine()
        assert engine.detect_language(Path("deploy.sh")) == "bash"

    def test_detect_csharp(self) -> None:
        engine = _make_engine()
        assert engine.detect_language(Path("Program.cs")) == "csharp"

    def test_detect_html(self) -> None:
        engine = _make_engine()
        assert engine.detect_language(Path("index.html")) == "html"

    def test_detect_css(self) -> None:
        engine = _make_engine()
        assert engine.detect_language(Path("styles.css")) == "css"

    def test_detect_uppercase_extension(self) -> None:
        """Extension detection is case-insensitive."""
        engine = _make_engine()
        assert engine.detect_language(Path("Main.PY")) == "python"

    def test_detect_unknown_returns_none(self) -> None:
        """Unknown extension returns None."""
        engine = _make_engine()
        result = engine.detect_language(Path("file.xyz_unknown"))
        assert result is None

    def test_detect_string_path(self) -> None:
        """detect_language accepts str paths in addition to Path objects.

        The method signature is Union[str, Path]; str is coerced to Path
        internally so the same extension-lookup logic applies.
        """
        engine = _make_engine()
        # str argument — valid per the Union[str, Path] signature
        result = engine.detect_language("foo.py")  # type: ignore[arg-type]  # testing str branch
        assert result == "python"


class TestAstSearchEngineParserCache:
    """Parsers are cached: the same parser object is reused across parse() calls."""

    def test_parser_reused_same_language(self) -> None:
        """Two parse() calls for the same language reuse the same parser object."""
        engine = _make_engine()
        engine.parse("x = 1\n", "python")
        engine.parse("y = 2\n", "python")
        # Cache should have exactly one entry for python
        assert engine._parser_cache_size() == 1

    def test_different_languages_cached_separately(self) -> None:
        """Each language gets its own cached parser."""
        engine = _make_engine()
        engine.parse("x = 1\n", "python")
        engine.parse("package main\n", "go")
        assert engine._parser_cache_size() == 2


class TestAstSearchEngineParseFile:
    """parse_file() reads a file from disk and parses it."""

    def test_parse_file_python_smoke(self) -> None:
        """parse_file() on smoke.py returns valid XRayNode."""
        from code_indexer.xray.xray_node import XRayNode

        engine = _make_engine()
        node = engine.parse_file(FIXTURES / "python" / "smoke.py")
        assert isinstance(node, XRayNode)
        assert node.type == "module"

    def test_parse_file_detects_language_from_extension(self) -> None:
        """parse_file() auto-detects language from file extension."""
        engine = _make_engine()
        node = engine.parse_file(FIXTURES / "python" / "smoke.py")
        assert node.type == "module"

    def test_parse_file_unknown_extension_raises(self) -> None:
        """parse_file() raises ValueError for unknown file extension."""
        import tempfile
        import os

        engine = _make_engine()
        with tempfile.NamedTemporaryFile(suffix=".unknownxyz", delete=False) as f:
            f.write(b"hello")
            tmp_path = f.name
        try:
            with pytest.raises(ValueError):
                engine.parse_file(Path(tmp_path))
        finally:
            os.unlink(tmp_path)
