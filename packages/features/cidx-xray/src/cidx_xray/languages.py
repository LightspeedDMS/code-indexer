"""Language registry for X-Ray AST search engine.

Maps canonical language names to tree-sitter-languages grammar names,
and file extensions to canonical language names.

NOTE: This module has no module-level imports of tree_sitter or
tree_sitter_languages.  Imports are deferred to function bodies called
only from AstSearchEngine.__init__() to preserve CLI startup time.
"""

# Mandatory languages: canonical name -> grammar name used by tree_sitter_languages
# Key insight: "csharp" is the CIDX canonical name, but the grammar is "c_sharp"
SUPPORTED_LANGUAGES: dict[str, str] = {
    "java": "java",
    "kotlin": "kotlin",
    "go": "go",
    "python": "python",
    "typescript": "typescript",
    "javascript": "javascript",
    "bash": "bash",
    "csharp": "c_sharp",
    "html": "html",
    "css": "css",
    "c": "c",
    "cpp": "cpp",
    "yaml": "yaml",
}

# ^ invariant EXTENSION_MAP keys = rust languages.rs:supported_extensions = its match arms; test:test_match_arms_list_and_python_map_are_equal
# ^ why search detection keeps languages absent from SUPPORTED_LANGUAGES, not dropped; xray-cli parses for searches
# ^ hazard adding sql to SUPPORTED_LANGUAGES => python tree != rust tree in AST dump; test:test_python_dump_kinds_equal_rust_kinds
EXTENSION_MAP: dict[str, str] = {
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".go": "go",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".sh": "bash",
    ".bash": "bash",
    ".cs": "csharp",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".c++": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".h++": "cpp",
    ".jsx": "javascript",
    ".tf": "terraform",
    ".hcl": "hcl",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".sql": "sql",
    ".xml": "xml",
    ".groovy": "groovy",
    ".gradle": "groovy",
    ".vue": "vue",
}

AST_DUMP_UNSUPPORTED_MESSAGE = "AST dump not supported for this language"


def _hcl_available() -> bool:
    """Return True if tree_sitter_languages bundles the HCL grammar.

    Used to conditionally add 'terraform' to the supported languages list
    at AstSearchEngine instantiation time.
    """
    try:
        from tree_sitter_languages import get_language

        get_language("hcl")
        return True
    except Exception:
        return False
