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
}

# File extension -> canonical language name mapping
# All keys are lowercase (matching Path.suffix.lower())
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
}


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
