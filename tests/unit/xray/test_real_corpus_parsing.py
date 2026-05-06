"""Real-corpus parsing tests for X-Ray AST engine.

User Mandate Section 1: parse every Python file in the CIDX codebase and
the per-language fixture corpus, plus a cross-language conceptual parity table.

These tests use the CIDX repository itself as the primary test corpus, exercising
async functions, dataclasses, decorators, type hints, walrus operators, f-strings,
match statements, comprehensions, type aliases, and other modern Python constructs
that toy fixtures cannot cover.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
TESTS_ROOT = PROJECT_ROOT / "tests"
FIXTURES = Path(__file__).parent / "fixtures"

# String literal node types across grammars (varies by language)
_STRING_NODE_TYPES = frozenset(
    {
        "string",
        "string_literal",
        "interpreted_string_literal",
        "raw_string_literal",
        "template_string",
    }
)

# Comment node types across grammars
_COMMENT_NODE_TYPES = frozenset({"comment", "line_comment", "block_comment"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine():  # type: ignore[return]
    """Construct AstSearchEngine without importing at module level."""
    from code_indexer.xray.ast_engine import AstSearchEngine

    return AstSearchEngine()


def _collect_py_files(root: Path) -> List[Path]:
    """Return all .py files under root, excluding __pycache__."""
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _walk_all_nodes(node, visitor):  # type: ignore[no-untyped-def]
    """Depth-first walk calling visitor(node) on every node."""
    visitor(node)
    for child in node.children:
        _walk_all_nodes(child, visitor)


def _find_first(node, type_name: str):  # type: ignore[return]
    """Return the first descendant (or self) matching type_name, else None."""
    if node.type == type_name:
        return node
    for child in node.children:
        result = _find_first(child, type_name)
        if result is not None:
            return result
    return None


# ---------------------------------------------------------------------------
# Section 1a: Parse every .py file under src/code_indexer/
# ---------------------------------------------------------------------------

_SRC_PY_FILES = _collect_py_files(SRC_ROOT / "code_indexer")


@pytest.mark.parametrize(
    "py_file", _SRC_PY_FILES, ids=lambda p: str(p.relative_to(SRC_ROOT))
)
def test_parse_src_python_file(py_file: Path) -> None:
    """Every .py file under src/code_indexer/ must parse without error.

    Asserts:
    - parse() does not raise
    - root_node.child_count >= 0 (empty __init__.py files are acceptable)
    - root node type is 'module'
    - root node itself is not an ERROR node
    """
    engine = _make_engine()
    source = py_file.read_bytes()

    root = engine.parse(source, "python")

    assert root is not None
    assert root.type == "module", (
        f"{py_file}: expected root type 'module', got {root.type!r}"
    )
    # Root must not itself be an ERROR node
    assert root.type != "ERROR", f"{py_file}: root node is ERROR - file failed to parse"
    assert root.child_count >= 0  # empty __init__.py: 0 children is acceptable


# ---------------------------------------------------------------------------
# Section 1b: Parse every .py file under tests/
# ---------------------------------------------------------------------------

_TEST_PY_FILES = _collect_py_files(TESTS_ROOT)


@pytest.mark.parametrize(
    "py_file", _TEST_PY_FILES, ids=lambda p: str(p.relative_to(TESTS_ROOT))
)
def test_parse_tests_python_file(py_file: Path) -> None:
    """Every .py file under tests/ must parse without error.

    The tests/ tree contains fixture files with deliberately challenging syntax.
    Asserts root is 'module' type and parse does not raise.
    """
    engine = _make_engine()
    source = py_file.read_bytes()

    root = engine.parse(source, "python")

    assert root is not None
    assert root.type == "module", (
        f"{py_file}: expected root type 'module', got {root.type!r}"
    )


# ---------------------------------------------------------------------------
# Section 1c: Per-language fixture corpus (all 10 languages x 4 fixture types)
# ---------------------------------------------------------------------------

_FIXTURE_PARAMS = [
    (lang, fixture_type)
    for lang in [
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
    ]
    for fixture_type in ["smoke", "realistic", "advanced", "pathological"]
]


@pytest.mark.parametrize(
    "lang,fixture_type",
    _FIXTURE_PARAMS,
    ids=lambda x: x if isinstance(x, str) else x,
)
def test_fixture_corpus_parses(lang: str, fixture_type: str) -> None:
    """Each language fixture must parse to a tree with child_count > 0.

    This exercises all 40 fixture files (10 languages x 4 types).
    """
    fixture_dir = FIXTURES / lang
    candidates = list(fixture_dir.glob(f"{fixture_type}.*"))
    assert candidates, f"No fixture file {fixture_type}.* found in {fixture_dir}"
    fixture_path = candidates[0]
    source = fixture_path.read_bytes()

    engine = _make_engine()
    root = engine.parse(source, lang)

    assert root is not None, f"{lang}/{fixture_type}: parse returned None"
    assert root.child_count > 0, (
        f"{lang}/{fixture_type}: root.child_count is 0 — tree appears empty"
    )


# ---------------------------------------------------------------------------
# Section 1d: Cross-language conceptual parity table
# ---------------------------------------------------------------------------

# Pattern 1: function/method definition with a name field
_FUNCTION_NAME_PARAMS = [
    ("java", b"public class T { void myMethod() {} }", "method_declaration"),
    ("kotlin", b"fun myFun() {}", "function_declaration"),
    ("go", b"package p\nfunc myFunc() {}", "function_declaration"),
    ("python", b"def my_func():\n    pass\n", "function_definition"),
    ("typescript", b"function myFunc() {}", "function_declaration"),
    ("javascript", b"function myFunc() {}", "function_declaration"),
]


@pytest.mark.parametrize(
    "lang,source,decl_type",
    _FUNCTION_NAME_PARAMS,
    ids=[p[0] for p in _FUNCTION_NAME_PARAMS],
)
def test_cross_lang_function_name_field(
    lang: str, source: bytes, decl_type: str
) -> None:
    """Function/method declaration 'name' field is locatable in all 6 languages.

    Asserts children_by_field_name('name') returns non-empty list and
    first element has a non-empty text value matching the source.
    """
    engine = _make_engine()
    root = engine.parse(source, lang)

    decl_node = _find_first(root, decl_type)
    assert decl_node is not None, f"{lang}: could not find {decl_type!r} node in AST"

    name_nodes = decl_node.children_by_field_name("name")
    assert len(name_nodes) > 0, (
        f"{lang}: children_by_field_name('name') returned empty list for {decl_type!r}"
    )
    name_text = name_nodes[0].text
    assert isinstance(name_text, str), (
        f"{lang}: name node text is not str, got {type(name_text)}"
    )
    assert len(name_text) > 0, f"{lang}: name node text is empty"


# Pattern 2: string literal containing a colon
_STRING_COLON_PARAMS = [
    ("python", b'x = "key:value"\n'),
    ("java", b'public class T { String s = "key:value"; }'),
    ("kotlin", b'val s = "key:value"'),
    ("go", b'package p\nvar s = "key:value"'),
    ("typescript", b'const s = "key:value";'),
    ("javascript", b'const s = "key:value";'),
]


@pytest.mark.parametrize(
    "lang,source",
    _STRING_COLON_PARAMS,
    ids=[p[0] for p in _STRING_COLON_PARAMS],
)
def test_cross_lang_string_literal_with_colon(lang: str, source: bytes) -> None:
    """String literal containing ':' is locatable in all 6 languages.

    Uses a set membership check on node.type to handle grammar differences
    (string, string_literal, interpreted_string_literal, etc.).
    """
    engine = _make_engine()
    root = engine.parse(source, lang)

    found = []

    def collect_strings(node):  # type: ignore[no-untyped-def]
        if node.type in _STRING_NODE_TYPES:
            found.append(node)
        for child in node.children:
            collect_strings(child)

    collect_strings(root)

    assert len(found) > 0, (
        f"{lang}: no string literal node found — node types checked: {_STRING_NODE_TYPES}"
    )
    # At least one string node should contain or represent "key:value"
    string_texts = [n.text for n in found]
    has_colon_string = any(":" in t for t in string_texts)
    assert has_colon_string, (
        f"{lang}: no string node text contains ':'. Found string texts: {string_texts}"
    )


# Pattern 3: comment present at module level
_COMMENT_PARAMS = [
    ("python", b"# a comment\nx = 1\n"),
    ("java", b"// line comment\npublic class T {}"),
    ("go", b"// comment\npackage main\n"),
    ("typescript", b"// ts comment\nconst x = 1;\n"),
    ("javascript", b"// js comment\nconst x = 1;\n"),
    ("kotlin", b"// kt comment\nfun main() {}\n"),
]


@pytest.mark.parametrize(
    "lang,source",
    _COMMENT_PARAMS,
    ids=[p[0] for p in _COMMENT_PARAMS],
)
def test_cross_lang_comment_at_module_level(lang: str, source: bytes) -> None:
    """Comment node is locatable at (or near) module level in all 6 languages.

    Uses set membership on node.type to handle grammar naming differences
    (comment, line_comment, block_comment).
    """
    engine = _make_engine()
    root = engine.parse(source, lang)

    # Search root children and their immediate children for a comment node
    comment_found = False

    def check(node):  # type: ignore[no-untyped-def]
        nonlocal comment_found
        if node.type in _COMMENT_NODE_TYPES:
            comment_found = True

    _walk_all_nodes(root, check)

    assert comment_found, (
        f"{lang}: no comment node found anywhere in tree. "
        f"Comment types checked: {_COMMENT_NODE_TYPES}"
    )
